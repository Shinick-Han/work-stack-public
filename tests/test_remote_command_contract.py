"""Focused tests for the skill transport argv contract (Packet A)."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_command_contract as CONTRACT

POSIX_PYTHON = "/workstack-fixture/command-owner/opt/python"
POSIX_INSTALL = "/workstack-fixture/command-owner/app"
POSIX_DATA = "/workstack-fixture/command-owner/data"
OWNER = "command_owner"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
CANARY = "CANARY_QX7M2"
SKILL_SCRIPT = f"{POSIX_INSTALL}/desktop/python-webview-shell/remote_skill_install.py"
FROZEN_PROVISION_TOKENS = [
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
]


class _Truthy:
    def __bool__(self) -> bool:
        return True


def expected_skill_tokens(*, apply: bool = False) -> list[str]:
    tokens = [
        POSIX_PYTHON,
        "-I",
        "-B",
        SKILL_SCRIPT,
        "--install-root",
        POSIX_INSTALL,
    ]
    if apply is True:
        tokens.append("--apply")
    return tokens


class RemoteSkillCommandContractTest(unittest.TestCase):
    def test_default_skill_tokens_are_read_only_helper_argv(self) -> None:
        tokens = CONTRACT.skill_tokens(
            remote_python=POSIX_PYTHON, remote_app_dir=POSIX_INSTALL
        )
        omitted = CONTRACT.skill_tokens(
            remote_python=POSIX_PYTHON, remote_app_dir=POSIX_INSTALL, apply=False
        )
        self.assertEqual(tokens, expected_skill_tokens())
        self.assertEqual(omitted, tokens)
        self.assertNotIn("--apply", tokens)
        self.assertNotIn("-", tokens)
        self.assertNotIn("--data-root", tokens)
        self.assertNotIn("--owner", tokens)
        self.assertNotIn("HOME", tokens)
        self.assertNotIn("-c", tokens)
        self.assertNotIn("provision-install", tokens)

    def test_apply_true_appends_only_the_apply_flag(self) -> None:
        tokens = CONTRACT.skill_tokens(
            remote_python=POSIX_PYTHON, remote_app_dir=POSIX_INSTALL, apply=True
        )
        self.assertEqual(tokens, expected_skill_tokens(apply=True))
        self.assertEqual(tokens[-1], "--apply")
        self.assertEqual(tokens[:-1], expected_skill_tokens())

    def test_join_skill_command_uses_the_shell_neutral_quoting_contract(self) -> None:
        tokens = CONTRACT.skill_tokens(
            remote_python=POSIX_PYTHON, remote_app_dir=POSIX_INSTALL
        )
        joined = CONTRACT.join_skill_command(
            remote_python=POSIX_PYTHON, remote_app_dir=POSIX_INSTALL
        )
        applied = CONTRACT.join_skill_command(
            remote_python=POSIX_PYTHON, remote_app_dir=POSIX_INSTALL, apply=True
        )
        provision = CONTRACT.join_provision_install_command(
            remote_python=POSIX_PYTHON,
            remote_app_dir=POSIX_INSTALL,
            remote_data_dir=POSIX_DATA,
            owner=OWNER,
            expected_workspace_uid=WORKSPACE_ID,
        )
        self.assertEqual(joined, " ".join(tokens))
        self.assertEqual(applied, " ".join(expected_skill_tokens(apply=True)))
        self.assertEqual(provision, " ".join(FROZEN_PROVISION_TOKENS))
        for remote in (joined, applied, provision):
            self.assertNotIn("'", remote)
            self.assertNotIn('"', remote)
            self.assertNotIn("&&", remote)
            self.assertNotIn("$(", remote)
            self.assertNotIn("'\"'\"'", remote)

    def test_non_bool_apply_and_unsafe_paths_are_refused(self) -> None:
        cases = (
            ("apply-string", {"apply": "true"}),
            ("apply-one", {"apply": 1}),
            ("apply-zero", {"apply": 0}),
            ("apply-object", {"apply": _Truthy()}),
            ("path-space", {"remote_app_dir": "/workstack-fixture/command owner/app"}),
            ("path-semicolon", {"remote_app_dir": "/tmp/app;" + CANARY}),
            ("path-substitution", {"remote_app_dir": "/tmp/app$(" + CANARY + ")"}),
            ("path-control", {"remote_app_dir": "/tmp/app\n" + CANARY}),
            ("path-trailing-slash", {"remote_app_dir": POSIX_INSTALL + "/"}),
            ("path-root", {"remote_app_dir": "/"}),
            ("path-dotdot", {"remote_app_dir": "/workstack-fixture/command-owner/../app"}),
            ("python-missing", {"remote_python": None}),
            ("python-empty", {"remote_python": ""}),
            ("python-bare", {"remote_python": "python3"}),
        )
        for label, overrides in cases:
            kwargs = {
                "remote_python": POSIX_PYTHON,
                "remote_app_dir": POSIX_INSTALL,
            }
            kwargs.update(overrides)
            with self.subTest(case=label):
                with mock.patch.object(subprocess, "Popen") as popen:
                    with mock.patch.object(subprocess, "run") as run:
                        with self.assertRaises(CONTRACT.RemoteCommandError) as raised:
                            CONTRACT.skill_tokens(**kwargs)
                        with self.assertRaises(CONTRACT.RemoteCommandError):
                            CONTRACT.join_skill_command(**kwargs)
                popen.assert_not_called()
                run.assert_not_called()
                message = str(raised.exception)
                self.assertNotIn(CANARY, message)
                if label in {"python-missing", "python-empty"}:
                    self.assertEqual(raised.exception.code, "REMOTE_PYTHON_REQUIRED")
                else:
                    self.assertEqual(raised.exception.code, "REMOTE_PROTOCOL_INVALID")

    def test_join_skill_command_still_refuses_an_embedded_ssh_token(self) -> None:
        with self.assertRaises(CONTRACT.RemoteCommandError) as raised:
            CONTRACT.join_skill_command(
                remote_python="/usr/bin/ssh", remote_app_dir=POSIX_INSTALL
            )
        self.assertEqual(raised.exception.code, "REMOTE_PROTOCOL_INVALID")
        self.assertIn("live ssh", str(raised.exception))
        CONTRACT.scan_tokens_for_live_ssh(["/opt/python", "-I", "-B"])
        with self.assertRaises(CONTRACT.RemoteCommandError):
            CONTRACT.scan_tokens_for_live_ssh(["/usr/bin/ssh"])

    def test_provision_install_tokens_remain_the_stdin_install_shape(self) -> None:
        tokens = CONTRACT.provision_install_tokens(
            remote_python=POSIX_PYTHON,
            remote_app_dir=POSIX_INSTALL,
            remote_data_dir=POSIX_DATA,
            owner=OWNER,
            expected_workspace_uid=WORKSPACE_ID,
        )
        self.assertEqual(tokens, FROZEN_PROVISION_TOKENS)
        self.assertEqual(tokens[3], "-")
        self.assertNotEqual(
            CONTRACT.skill_tokens(
                remote_python=POSIX_PYTHON, remote_app_dir=POSIX_INSTALL
            ),
            tokens,
        )


if __name__ == "__main__":
    unittest.main()
