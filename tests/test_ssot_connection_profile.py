from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "ssot_connection.py"
SPEC = importlib.util.spec_from_file_location("ssot_connection_profile_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def remote_draft(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "storage_mode": "ssh-remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "local_forward_port": 18765,
        "workspace_id": WORKSPACE_ID,
    }
    payload.update(overrides)
    return payload


class SsotConnectionProfileTest(unittest.TestCase):
    def test_absent_profile_preserves_local_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(MODULE.load_connection_draft(root), {"storage_mode": "local"})
            self.assertIsNone(MODULE.load_remote_connection_profile(root))

    def test_profile_defaults_remote_port_and_matches_legacy_shape(self) -> None:
        normalized = MODULE.validate_connection_draft(remote_draft())
        profile = MODULE.connection_profile_from_draft(normalized)

        self.assertEqual(normalized["remote_port"], 8765)
        self.assertEqual(profile.ssh_host_alias, "work-linux")
        self.assertEqual(profile.remote_app_dir, "/srv/workstack/app")
        self.assertEqual(profile.remote_data_dir, "/srv/workstack/ssot")
        self.assertEqual(profile.local_forward_port, 18765)
        self.assertEqual(profile.workspace_id, WORKSPACE_ID)
        self.assertEqual(profile.remote_port, 8765)

    def test_profile_rejects_shell_alias_paths_ports_identity_and_extra_fields(self) -> None:
        cases = (
            {"ssh_host_alias": "work-linux; calc.exe"},
            {"ssh_host_alias": "-V"},
            {"ssh_host_alias": "-F"},
            {"remote_app_dir": "relative/path"},
            {"remote_data_dir": "/srv/workstack/../private"},
            {"remote_data_dir": "/"},
            {"local_forward_port": True},
            {"workspace_id": "not-a-uuid"},
            {"surprise": "field"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(RuntimeError):
                MODULE.validate_connection_draft(remote_draft(**overrides))

    def test_local_draft_is_strict(self) -> None:
        self.assertEqual(
            MODULE.validate_connection_draft({"storage_mode": "local"}),
            {"storage_mode": "local"},
        )
        with self.assertRaisesRegex(RuntimeError, "unsupported fields"):
            MODULE.validate_connection_draft({"storage_mode": "local", "ssh_host_alias": "x"})

    def test_save_is_canonical_atomic_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized = MODULE.save_connection_draft(root, remote_draft())
            path = root / MODULE.REMOTE_CONNECTION_FILE

            self.assertEqual(MODULE.load_connection_draft(root), normalized)
            self.assertEqual(path.read_text(encoding="utf-8"), json.dumps(
                normalized, ensure_ascii=True, separators=(",", ":")
            ) + "\n")
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_tunnel_command_is_loopback_only_strict_and_fixed_shape(self) -> None:
        profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/srv/workstack/app files", "/srv/workstack/private ssot",
            18765, WORKSPACE_ID, 9876,
        )
        command = MODULE.build_ssh_tunnel_command(profile, "ssh")

        self.assertIn("127.0.0.1:18765:127.0.0.1:9876", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)
        self.assertEqual(command[-3], "--")
        self.assertEqual(command[-2], "work-linux")
        self.assertIn("exec python3", command[-1])
        self.assertIn("--host 127.0.0.1", command[-1])
        self.assertIn("--public-port 18765", command[-1])
        self.assertIn("--exit-with-parent", command[-1])
        self.assertIn("'/srv/workstack/private ssot'", command[-1])

    def test_check_command_is_read_only(self) -> None:
        profile = MODULE.RemoteConnectionProfile("work-linux", "/app", "/ssot", 18765, WORKSPACE_ID)
        command = MODULE.build_ssh_check_command(profile, "ssh")

        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(command[-3], "--")
        self.assertIn("test -f /app/run_work_stack.py", command[-1])
        self.assertIn("test -d /ssot", command[-1])
        self.assertIn("python3 /app/run_work_stack.py --help", command[-1])
        for mutating_word in ("mkdir", " rm ", " mv ", " cp ", "chmod", "chown"):
            self.assertNotIn(mutating_word, command[-1])

    @mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh")
    @mock.patch.object(MODULE.subprocess, "run")
    def test_read_only_check_reports_failure_without_echoing_command(
        self, run: mock.Mock, _find: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 255, "", "Host key verification failed.")
        with self.assertRaisesRegex(RuntimeError, "known-host key"):
            MODULE.run_remote_connection_check(
                MODULE.connection_profile_from_draft(remote_draft())
            )

    def test_runtime_port_preserves_available_preference(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind((MODULE.LOOPBACK_HOST, 0))
            port = int(candidate.getsockname()[1])
        self.assertEqual(MODULE.resolve_runtime_forward_port(port), port)

    def test_runtime_port_falls_back_without_probing_occupant_or_persisting(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
            occupant.bind((MODULE.LOOPBACK_HOST, 0))
            occupant.listen(1)
            occupied_port = int(occupant.getsockname()[1])
            with mock.patch.object(MODULE.socket.socket, "connect", side_effect=AssertionError("probe")):
                selected = MODULE.resolve_runtime_forward_port(occupied_port)

        self.assertNotEqual(selected, occupied_port)
        self.assertGreater(selected, 0)
        profile = MODULE.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", selected, WORKSPACE_ID
        )
        command = MODULE.build_ssh_tunnel_command(profile, "ssh")
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn(f"127.0.0.1:{selected}:127.0.0.1:8765", command)

    def test_runtime_profile_does_not_mutate_persisted_preference(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupant:
            occupant.bind((MODULE.LOOPBACK_HOST, 0))
            occupant.listen(1)
            occupied_port = int(occupant.getsockname()[1])
            original = MODULE.RemoteConnectionProfile(
                "work-linux", "/app", "/ssot", occupied_port, WORKSPACE_ID
            )
            runtime = MODULE.profile_with_runtime_forward_port(original)

        self.assertEqual(original.local_forward_port, occupied_port)
        self.assertNotEqual(runtime.local_forward_port, occupied_port)


if __name__ == "__main__":
    unittest.main()
