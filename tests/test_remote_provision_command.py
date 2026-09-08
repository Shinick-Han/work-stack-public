"""Focused tests for the pure provision-install argv/SSH command producer."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
COMMAND_PATH = SHELL / "remote_provision_command.py"
CONTRACT_PATH = SHELL / "remote_command_contract.py"
INSTALLER_PATH = SHELL / "remote_provision_installer.py"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_command_contract as CONTRACT
import remote_provision_command as COMMAND

INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "remote_provision_installer_command_comp", INSTALLER_PATH
)
assert INSTALLER_SPEC is not None and INSTALLER_SPEC.loader is not None
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(INSTALLER)

POSIX_PYTHON = "/workstack-fixture/command-owner/opt/python"
POSIX_INSTALL = "/workstack-fixture/command-owner/app"
POSIX_DATA = "/workstack-fixture/command-owner/data"
OWNER = "command_owner"
ALIAS = "fixture-linux"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_TOKEN = "r5pending-token-not-enforced-01"
NIL_UID = "00000000-0000-0000-0000-000000000000"
NCS_UID = "11111111-1111-4111-0111-111111111111"
UPPER_UID = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
CANARY = "CANARY_QX7M2"
SSH_EXECUTABLE = "ssh.exe"
FROZEN_SSH_PREFIX = [
    SSH_EXECUTABLE,
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "PermitLocalCommand=no",
    "-o",
    "ClearAllForwardings=yes",
    "--",
    ALIAS,
]


def load_profile(**overrides: object) -> SimpleNamespace:
    values = {
        "remote_python": POSIX_PYTHON,
        "remote_app_dir": POSIX_INSTALL,
        "remote_data_dir": POSIX_DATA,
        "expected_workspace_id": WORKSPACE_ID,
        "ssh_host_alias": ALIAS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class RemoteProvisionInstallCommandTest(unittest.TestCase):
    def test_valid_producer_tail_matches_installer_parse_argv(self) -> None:
        command = COMMAND.build_ssh_provision_install_command(
            load_profile(), OWNER, SSH_EXECUTABLE
        )
        self.assertEqual(command[:14], FROZEN_SSH_PREFIX)
        self.assertIsNot(getattr(command, "shell", None), True)
        remote = command[-1]
        tokens = remote.split()
        self.assertEqual(
            tokens,
            [
                POSIX_PYTHON,
                "-I",
                "-B",
                "-",
                "provision-install",
                "--install-root",
                POSIX_INSTALL,
                "--data-root",
                POSIX_DATA,
                "--owner",
                OWNER,
                "--expected-workspace-uid",
                WORKSPACE_ID,
            ],
        )
        self.assertEqual(tokens[4:], list(tokens[-9:]))
        self.assertEqual(INSTALLER.COMMAND, CONTRACT.PROVISION_INSTALL_COMMAND)
        parsed = INSTALLER._parse_argv(tokens[-9:])
        self.assertEqual(parsed, (POSIX_INSTALL, POSIX_DATA, OWNER, WORKSPACE_ID))
        self.assertNotIn("-c", tokens)
        self.assertNotIn("'", remote)
        self.assertNotIn('"', remote)
        self.assertNotIn("&&", remote)
        self.assertNotIn("$(", remote)
        self.assertNotIn("2>&1", remote)
        self.assertNotIn("remote_entry.py", remote)

    def test_invalid_inputs_are_refused_without_subprocess(self) -> None:
        cases = (
            ("python-missing", load_profile(remote_python=None), OWNER, SSH_EXECUTABLE),
            ("python-empty", load_profile(remote_python=""), OWNER, SSH_EXECUTABLE),
            ("python-bare", load_profile(remote_python="python3"), OWNER, SSH_EXECUTABLE),
            (
                "python-relative",
                load_profile(remote_python="venv/bin/python"),
                OWNER,
                SSH_EXECUTABLE,
            ),
            ("owner-space", load_profile(), "bad owner", SSH_EXECUTABLE),
            ("owner-injection", load_profile(), f"root;{CANARY}", SSH_EXECUTABLE),
            ("owner-leading-digit", load_profile(), "1owner", SSH_EXECUTABLE),
            ("uid-nil", load_profile(expected_workspace_id=NIL_UID), OWNER, SSH_EXECUTABLE),
            ("uid-ncs", load_profile(expected_workspace_id=NCS_UID), OWNER, SSH_EXECUTABLE),
            (
                "uid-upper",
                load_profile(expected_workspace_id=UPPER_UID),
                OWNER,
                SSH_EXECUTABLE,
            ),
            (
                "path-dotdot",
                load_profile(remote_app_dir="/workstack-fixture/command-owner/../app"),
                OWNER,
                SSH_EXECUTABLE,
            ),
            (
                "path-dot",
                load_profile(remote_data_dir="/workstack-fixture/command-owner/./data"),
                OWNER,
                SSH_EXECUTABLE,
            ),
            (
                "path-trailing-slash",
                load_profile(remote_app_dir=POSIX_INSTALL + "/"),
                OWNER,
                SSH_EXECUTABLE,
            ),
            ("path-root", load_profile(remote_app_dir="/"), OWNER, SSH_EXECUTABLE),
            (
                "roots-equal",
                load_profile(remote_data_dir=POSIX_INSTALL),
                OWNER,
                SSH_EXECUTABLE,
            ),
            (
                "roots-nested",
                load_profile(remote_data_dir=POSIX_INSTALL + "/nested"),
                OWNER,
                SSH_EXECUTABLE,
            ),
            (
                "alias-option",
                load_profile(ssh_host_alias="-oProxyCommand=" + CANARY),
                OWNER,
                SSH_EXECUTABLE,
            ),
            ("alias-dash-v", load_profile(ssh_host_alias="-V"), OWNER, SSH_EXECUTABLE),
            (
                "alias-semicolon",
                load_profile(ssh_host_alias="host;" + CANARY),
                OWNER,
                SSH_EXECUTABLE,
            ),
            (
                "alias-substitution",
                load_profile(ssh_host_alias="host$(" + CANARY + ")"),
                OWNER,
                SSH_EXECUTABLE,
            ),
            ("alias-empty", load_profile(ssh_host_alias=""), OWNER, SSH_EXECUTABLE),
            (
                "alias-control",
                load_profile(ssh_host_alias="host\n" + CANARY),
                OWNER,
                SSH_EXECUTABLE,
            ),
            ("executable-empty", load_profile(), OWNER, ""),
        )
        for label, profile, owner, executable in cases:
            with self.subTest(case=label):
                with mock.patch.object(subprocess, "Popen") as popen:
                    with mock.patch.object(subprocess, "run") as run:
                        with self.assertRaises(CONTRACT.RemoteCommandError) as raised:
                            COMMAND.build_ssh_provision_install_command(
                                profile, owner, executable
                            )
                popen.assert_not_called()
                run.assert_not_called()
                message = str(raised.exception)
                self.assertNotIn(CANARY, message)
                self.assertNotIn("ProxyCommand", message)
                self.assertNotIn(POSIX_INSTALL, message)
                self.assertNotIn(POSIX_DATA, message)
                self.assertNotIn(POSIX_PYTHON, message)
                if label == "python-missing" or label == "python-empty":
                    self.assertEqual(raised.exception.code, "REMOTE_PYTHON_REQUIRED")
                else:
                    self.assertEqual(raised.exception.code, "REMOTE_PROTOCOL_INVALID")

    def test_old_verbs_are_unchanged(self) -> None:
        probe = CONTRACT.probe_tokens(
            remote_python=POSIX_PYTHON,
            remote_app_dir=POSIX_INSTALL,
            remote_data_dir=POSIX_DATA,
        )
        serve = CONTRACT.serve_tokens(
            remote_python=POSIX_PYTHON,
            remote_app_dir=POSIX_INSTALL,
            remote_data_dir=POSIX_DATA,
            remote_port=8765,
            local_forward_port=18765,
            session_token=SESSION_TOKEN,
        )
        stop = CONTRACT.stop_owned_tokens(
            remote_python=POSIX_PYTHON,
            remote_app_dir=POSIX_INSTALL,
            remote_data_dir=POSIX_DATA,
            session_token=SESSION_TOKEN,
        )
        self.assertEqual(
            probe,
            [
                POSIX_PYTHON,
                "-I",
                "-B",
                f"{POSIX_INSTALL}/desktop/python-webview-shell/remote_entry.py",
                "probe",
                "--app-dir",
                POSIX_INSTALL,
                "--data-dir",
                POSIX_DATA,
            ],
        )
        self.assertEqual(serve[:2], [CONTRACT.SERVE_EXEC_PREFIX, POSIX_PYTHON])
        self.assertEqual(serve[5], "serve")
        self.assertEqual(stop[0], POSIX_PYTHON)
        self.assertEqual(stop[4], "stop-owned")
        self.assertNotIn(CONTRACT.SERVE_EXEC_PREFIX, probe)
        self.assertNotIn(CONTRACT.SERVE_EXEC_PREFIX, stop)
        self.assertNotEqual(probe[3], "-")
        self.assertTrue(probe[3].endswith("/desktop/python-webview-shell/remote_entry.py"))
        self.assertNotIn("provision-install", probe)
        self.assertNotIn("provision-install", serve)
        self.assertNotIn("provision-install", stop)
        self.assertNotIn("-c", probe)
        self.assertNotIn("-c", serve)
        self.assertNotIn("-c", stop)
        joined = CONTRACT.join_probe_command(
            remote_python=POSIX_PYTHON,
            remote_app_dir=POSIX_INSTALL,
            remote_data_dir=POSIX_DATA,
        )
        self.assertEqual(joined, " ".join(probe))

    def test_module_is_a_pure_builder(self) -> None:
        self.assertEqual(imported_modules(COMMAND_PATH), {"__future__", "remote_command_contract"})
        source = COMMAND_PATH.read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("discover_ssh_host_aliases", source)
        self.assertNotIn("Popen", source)
        contract_source = CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertIn("SAFE_TOKEN_PATTERN = re.compile(r\"^[A-Za-z0-9._/=:-]+$\")", contract_source)
        self.assertNotIn("staging", contract_source)
        with mock.patch.object(subprocess, "Popen") as popen:
            with mock.patch.object(subprocess, "run") as run:
                COMMAND.build_ssh_provision_install_command(
                    load_profile(), OWNER, SSH_EXECUTABLE
                )
        popen.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
