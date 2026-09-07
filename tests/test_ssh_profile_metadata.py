from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))
SPEC = importlib.util.spec_from_file_location(
    "ssh_profile_metadata_test", SHELL / "ssh_profile_metadata.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"


def profile(**overrides: object):
    values = {
        "profile_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "label": "Remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "expected_workspace_id": WORKSPACE_ID,
        "preferred_forward_port": 18765,
        "remote_python": "/srv/workstack/venv/bin/python",
    }
    values.update(overrides)
    return MODULE.SshConnectionProfile(**values)


class FakeProcess:
    def __init__(self, payload: bytes, returncode: int = 0) -> None:
        import io

        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(b"")
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class SshProfileMetadataTest(unittest.TestCase):
    def test_command_is_fixed_shape_and_uses_shared_probe_entry(self) -> None:
        command = MODULE.build_ssh_profile_metadata_command(profile(), "ssh.exe")

        self.assertEqual(command[:14], [
            "ssh.exe", "-T", "-o", "BatchMode=yes", "-o",
            "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o",
            "PermitLocalCommand=no", "-o", "ClearAllForwardings=yes", "--", "work-linux",
        ])
        remote = command[-1]
        self.assertIn("/srv/workstack/venv/bin/python", remote)
        self.assertIn("desktop/python-webview-shell/remote_entry.py", remote)
        self.assertIn(" probe ", f" {remote} ")
        self.assertNotIn("python3 -I -B -c", remote)
        self.assertNotIn("-c", remote.split())
        self.assertNotIn("mkdir", remote)
        self.assertNotIn("run_work_stack.py", remote)
        self.assertNotIn("&&", remote)

    def test_missing_remote_python_fails_before_popen(self) -> None:
        with mock.patch.object(MODULE.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "REMOTE_PYTHON_REQUIRED"):
                MODULE.run_remote_profile_metadata_check(
                    profile(remote_python=None), ssh_executable="ssh.exe"
                )
        popen.assert_not_called()

    def test_valid_bounded_metadata_is_returned(self) -> None:
        payload = json.dumps({
            "workspace_id": WORKSPACE_ID,
            "product_version": "1.0.6",
            "protocol_version": 1,
        }).encode()
        process = FakeProcess(payload)

        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            result = MODULE.run_remote_profile_metadata_check(
                profile(), ssh_executable="ssh.exe"
            )

        self.assertEqual(result.actual_workspace_id, WORKSPACE_ID)
        self.assertEqual(result.product_version, "1.0.6")
        self.assertEqual(result.protocol_version, 1)

    def test_oversized_output_is_killed_and_never_parsed(self) -> None:
        process = FakeProcess(b"x" * (MODULE.MAX_METADATA_BYTES + 1))
        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "safe limit"):
                MODULE.run_remote_profile_metadata_check(profile(), ssh_executable="ssh.exe")
        self.assertTrue(process.killed)

    def test_failure_output_is_sanitized(self) -> None:
        process = FakeProcess(b"company-secret", returncode=255)
        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "metadata check failed") as caught:
                MODULE.run_remote_profile_metadata_check(profile(), ssh_executable="ssh.exe")
        self.assertNotIn("company-secret", str(caught.exception))

    def test_stable_lock_owned_code_is_surfaced_without_raw_token(self) -> None:
        import io

        process = FakeProcess(b"", returncode=2)
        process.stderr = io.BytesIO(b"REMOTE_LOCK_OWNED: pid=4242\n")
        with mock.patch.object(MODULE.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "REMOTE_LOCK_OWNED") as caught:
                MODULE.run_remote_profile_metadata_check(profile(), ssh_executable="ssh.exe")
        self.assertNotIn("r5pending", str(caught.exception))
        self.assertNotIn("session-token", str(caught.exception).lower())

    def test_invalid_metadata_shape_and_values_fail_closed(self) -> None:
        bad_values = [
            {"workspace_id": WORKSPACE_ID, "product_version": "1.0.6"},
            {"workspace_id": "bad", "product_version": "1.0.6", "protocol_version": 1},
            {"workspace_id": WORKSPACE_ID, "product_version": "", "protocol_version": 1},
            {"workspace_id": WORKSPACE_ID, "product_version": "1.0.6", "protocol_version": True},
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    MODULE._parse_metadata(json.dumps(value).encode())


if __name__ == "__main__":
    unittest.main()
