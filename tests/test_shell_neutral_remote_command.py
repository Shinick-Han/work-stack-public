"""Wave 1 R1: login-shell-neutral remote command contract (green).

Plan: WORKSTACK-REMOTE-SSH-SHELL-NEUTRAL-PLAN-2026-09-04.ko.md §4 R0–R1, §8.1.
Wave 0 receipts remain historical evidence of the 1.0.7 RED characterization.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
REQUIRED_REMOTE_PYTHON = "/srv/workstack/venv/bin/python"
RUNTIME_SESSION_TOKEN = "r5pending-token-not-enforced-01"

FORBIDDEN_FRAGMENTS = (
    "&&",
    "||",
    ";",
    "2>&1",
    ">/dev/null",
    "$(",
    "`",
    "bash -lc",
    "python -c",
    "python3 -c",
    "cd --",
)

POSIX_QUOTE_DANCE = "'\"'\"'"

if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SSOT = _load("ssot_connection_r0", SHELL / "ssot_connection.py")
META = _load("ssh_profile_metadata_r0", SHELL / "ssh_profile_metadata.py")
REGISTRY = _load("connection_registry_r0", SHELL / "connection_registry.py")


def ssot_profile():
    return SSOT.RemoteConnectionProfile(
        "work-linux",
        "/srv/workstack/app",
        "/srv/workstack/ssot",
        18765,
        WORKSPACE_ID,
        8765,
        REQUIRED_REMOTE_PYTHON,
    )


def metadata_profile(**overrides: object):
    values = {
        "profile_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "label": "Remote",
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "expected_workspace_id": WORKSPACE_ID,
        "preferred_forward_port": 18765,
        "remote_python": REQUIRED_REMOTE_PYTHON,
    }
    values.update(overrides)
    return META.SshConnectionProfile(**values)


def ssh_registry_profile(**overrides: object):
    values = {
        "profile_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "label": "Remote",
        "kind": "ssh",
        "enabled": True,
        "live_updates": True,
        "ssh_host_alias": "work-linux",
        "remote_app_dir": "/srv/workstack/app",
        "remote_data_dir": "/srv/workstack/ssot",
        "expected_workspace_id": WORKSPACE_ID,
        "preferred_forward_port": 18765,
        "remote_port": 8765,
        "remote_python": REQUIRED_REMOTE_PYTHON,
    }
    values.update(overrides)
    return values


def registry_document(profile: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_profile_id": profile["profile_id"],
        "profiles": [profile],
    }


def forbidden_in(command: str) -> tuple[str, ...]:
    return tuple(fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in command)


def bare_python3_tokens(command: str) -> list[str]:
    return re.findall(r"(?<![A-Za-z0-9_./-])python3(?![A-Za-z0-9_./-])", command)


def bash_words(command: str) -> list[str]:
    return shlex.split(command, posix=True)


def csh_words(command: str) -> list[str]:
    """Mock csh/tcsh tokenization of an OpenSSH remote command string."""
    if "2>&1" in command:
        raise RuntimeError("csh: Ambiguous output redirect")
    if POSIX_QUOTE_DANCE in command:
        raise RuntimeError("csh: POSIX quote-dance; parentheses escape as Badly placed ()'s")
    if "&&" in command or "||" in command:
        raise RuntimeError("csh: POSIX list operator is not a csh token")
    return shlex.split(command, posix=True)


def tcsh_words(command: str) -> list[str]:
    return csh_words(command)


def serve_command(profile=None):
    return SSOT.build_remote_server_command(
        profile or ssot_profile(), session_token=RUNTIME_SESSION_TOKEN
    )


class ShellNeutralRemoteCommandContractTest(unittest.TestCase):
    def test_r0_serve_command_has_zero_shell_operators_or_bare_python3(self) -> None:
        remote = serve_command()
        self.assertEqual(forbidden_in(remote), ())
        self.assertEqual(bare_python3_tokens(remote), [])
        self.assertNotIn("-c", bash_words(remote))
        self.assertIn(REQUIRED_REMOTE_PYTHON, remote)
        self.assertIn("desktop/python-webview-shell/remote_entry.py", remote)

    def test_r0_check_command_has_zero_redirects_or_list_operators(self) -> None:
        remote = SSOT.build_ssh_check_command(ssot_profile(), "ssh")[-1]
        self.assertEqual(forbidden_in(remote), ())
        self.assertEqual(bare_python3_tokens(remote), [])
        self.assertNotIn("command -v", remote)
        self.assertIn(" probe ", f" {remote} ")

    def test_r0_metadata_command_does_not_use_python_dash_c(self) -> None:
        remote = META.build_ssh_profile_metadata_command(metadata_profile(), "ssh.exe")[-1]
        self.assertEqual(forbidden_in(remote), ())
        self.assertNotIn("-c", bash_words(remote))
        self.assertEqual(bare_python3_tokens(remote), [])
        self.assertNotIn(POSIX_QUOTE_DANCE, remote)
        self.assertIn(" probe ", f" {remote} ")

    def test_r0_login_shells_observe_identical_argv(self) -> None:
        serve = serve_command()
        check = SSOT.build_ssh_check_command(ssot_profile(), "ssh")[-1]
        metadata = META.build_ssh_profile_metadata_command(metadata_profile(), "ssh.exe")[-1]
        for label, remote in (("serve", serve), ("check", check), ("metadata", metadata)):
            with self.subTest(command=label):
                bash = bash_words(remote)
                try:
                    csh = csh_words(remote)
                    tcsh = tcsh_words(remote)
                except RuntimeError as error:
                    self.fail(f"{label} argv is not login-shell-neutral: {error}")
                self.assertEqual(bash, csh)
                self.assertEqual(csh, tcsh)
                self.assertGreater(len(bash), 0)

    def test_r0_new_ssh_profile_requires_absolute_remote_python(self) -> None:
        try:
            parsed = REGISTRY.registry_from_document(registry_document(ssh_registry_profile()))
        except RuntimeError as error:
            self.fail(f"SSH registry still rejects remote_python: {error}")
        ssh = parsed.profiles[0]
        self.assertEqual(getattr(ssh, "remote_python"), REQUIRED_REMOTE_PYTHON)

    def test_r0_legacy_ssh_profile_without_remote_python_remains_readable(self) -> None:
        payload = ssh_registry_profile()
        del payload["remote_python"]
        parsed = REGISTRY.registry_from_document(registry_document(payload))
        self.assertIsNone(parsed.profiles[0].remote_python)
        with mock.patch.object(META.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "REMOTE_PYTHON_REQUIRED"):
                META.build_ssh_profile_metadata_command(parsed.profiles[0], "ssh.exe")
            with self.assertRaisesRegex(RuntimeError, "REMOTE_PYTHON_REQUIRED"):
                SSOT.build_remote_server_command(
                    SSOT.RemoteConnectionProfile(
                        parsed.profiles[0].ssh_host_alias,
                        parsed.profiles[0].remote_app_dir,
                        parsed.profiles[0].remote_data_dir,
                        parsed.profiles[0].preferred_forward_port,
                        parsed.profiles[0].expected_workspace_id,
                        parsed.profiles[0].remote_port,
                        parsed.profiles[0].remote_python,
                    )
                )
        popen.assert_not_called()

    def test_r0_ssot_draft_requires_remote_python(self) -> None:
        draft = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/srv/workstack/app",
            "remote_data_dir": "/srv/workstack/ssot",
            "local_forward_port": 18765,
            "workspace_id": WORKSPACE_ID,
            "remote_python": REQUIRED_REMOTE_PYTHON,
        }
        try:
            normalized = SSOT.validate_connection_draft(draft)
        except RuntimeError as error:
            self.fail(f"SSOT draft still rejects remote_python: {error}")
        self.assertEqual(normalized["remote_python"], REQUIRED_REMOTE_PYTHON)

    def test_r0_ssot_draft_without_remote_python_is_rejected(self) -> None:
        draft = {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": "work-linux",
            "remote_app_dir": "/srv/workstack/app",
            "remote_data_dir": "/srv/workstack/ssot",
            "local_forward_port": 18765,
            "workspace_id": WORKSPACE_ID,
        }
        with self.assertRaisesRegex(RuntimeError, "remote_python"):
            SSOT.validate_connection_draft(draft)

    def test_r0_unsafe_python_and_paths_fail_before_subprocess(self) -> None:
        cases = (
            ssot_profile().__class__(
                "work-linux",
                "/srv/workstack/app",
                "/srv/workstack/ssot",
                18765,
                WORKSPACE_ID,
                8765,
                "python3",
            ),
            ssot_profile().__class__(
                "work-linux",
                "/srv/workstack/app",
                "/srv/workstack/ssot",
                18765,
                WORKSPACE_ID,
                8765,
                "venv/bin/python",
            ),
            ssot_profile().__class__(
                "work-linux",
                "/srv/work stack/app",
                "/srv/workstack/ssot",
                18765,
                WORKSPACE_ID,
                8765,
                REQUIRED_REMOTE_PYTHON,
            ),
        )
        for profile in cases:
            with self.subTest(remote_python=profile.remote_python, app=profile.remote_app_dir):
                with mock.patch.object(SSOT.subprocess, "run") as run:
                    with self.assertRaises(RuntimeError):
                        SSOT.build_ssh_check_command(profile, "ssh")
                    with self.assertRaises(RuntimeError):
                        SSOT.run_remote_connection_check(profile)
                run.assert_not_called()

    def test_r1u_root_paths_are_rejected_before_subprocess(self) -> None:
        roots = (
            ("remote_app_dir", "/", "/srv/workstack/ssot", REQUIRED_REMOTE_PYTHON),
            ("remote_data_dir", "/srv/workstack/app", "/", REQUIRED_REMOTE_PYTHON),
            ("remote_python", "/srv/workstack/app", "/srv/workstack/ssot", "/"),
        )
        for label, app, data, python in roots:
            with self.subTest(field=label):
                profile = SSOT.RemoteConnectionProfile(
                    "work-linux", app, data, 18765, WORKSPACE_ID, 8765, python
                )
                with mock.patch.object(SSOT.subprocess, "run") as run:
                    with self.assertRaisesRegex(RuntimeError, "root"):
                        SSOT.build_ssh_check_command(profile, "ssh")
                    with self.assertRaisesRegex(RuntimeError, "root"):
                        serve_command(profile)
                run.assert_not_called()

    def test_r1u_missing_or_all_zero_session_token_is_explicit_r5_gap(self) -> None:
        profile = ssot_profile()
        with self.assertRaisesRegex(RuntimeError, "R5_OWNERSHIP_NOT_IMPLEMENTED"):
            SSOT.build_remote_server_command(profile)
        with self.assertRaisesRegex(RuntimeError, "REMOTE_SESSION_TOKEN_INVALID"):
            SSOT.build_remote_server_command(profile, session_token="0" * 32)
        remote = serve_command(profile)
        self.assertIn(RUNTIME_SESSION_TOKEN, remote)
        self.assertNotIn("00000000000000000000000000000000", remote)

    def test_r6_serve_execs_the_absolute_interpreter_in_every_login_shell(self) -> None:
        """Without `exec` a csh login shell keeps a forked child between sshd and the
        server, so PR_SET_PDEATHSIG watches that shell and an abrupt tunnel loss
        leaves the server running."""

        remote = serve_command()
        self.assertEqual(forbidden_in(remote), ())
        for words in (bash_words(remote), csh_words(remote), tcsh_words(remote)):
            self.assertEqual(words[0], "exec")
            self.assertEqual(words[1], REQUIRED_REMOTE_PYTHON)
            self.assertEqual(words.count("exec"), 1)
        self.assertEqual(bare_python3_tokens(remote), [])
        self.assertNotIn("exec python3", remote)

    def test_r6_read_only_verbs_carry_no_exec_and_reject_a_misplaced_one(self) -> None:
        check = SSOT.build_ssh_check_command(ssot_profile(), "ssh")[-1]
        stop = SSOT.build_ssh_stop_owned_command(ssot_profile(), "ssh", RUNTIME_SESSION_TOKEN)[-1]
        metadata = META.build_ssh_profile_metadata_command(metadata_profile(), "ssh.exe")[-1]
        for label, remote in (("check", check), ("stop", stop), ("metadata", metadata)):
            with self.subTest(command=label):
                self.assertNotIn("exec", bash_words(remote))
        contract = sys.modules[SSOT.join_probe_command.__module__]
        with self.assertRaisesRegex(RuntimeError, "exec"):
            contract.join_remote_tokens([REQUIRED_REMOTE_PYTHON, "exec", "/etc/passwd"])
        with self.assertRaisesRegex(RuntimeError, "exec"):
            contract.join_remote_tokens(["exec", "venv/bin/python"])
        with self.assertRaisesRegex(RuntimeError, "exec"):
            contract.join_remote_tokens(["exec"])
        with self.assertRaisesRegex(RuntimeError, "bare python interpreter"):
            contract.join_remote_tokens(["exec", "python3"])
        with self.assertRaisesRegex(RuntimeError, "exec"):
            contract.join_remote_tokens(["exec", "/usr/bin/id"])
        with self.assertRaisesRegex(RuntimeError, "exec"):
            contract.join_exact_serve_tokens(["exec", "/usr/bin/id"])
        serve = SSOT.build_remote_server_command(ssot_profile(), session_token=RUNTIME_SESSION_TOKEN)
        self.assertEqual(bash_words(serve)[:2], ["exec", REQUIRED_REMOTE_PYTHON])

    def test_r6_self_probe_token_rides_only_the_owned_target(self) -> None:
        owned = ssot_profile()
        remote = SSOT.build_ssh_check_command(owned, "ssh", RUNTIME_SESSION_TOKEN)[-1]
        self.assertEqual(forbidden_in(remote), ())
        self.assertIn(" probe ", f" {remote} ")
        self.assertEqual(bash_words(remote)[-2:], ["--session-token", RUNTIME_SESSION_TOKEN])
        self.assertEqual(bash_words(remote), csh_words(remote))
        anonymous = SSOT.build_ssh_check_command(owned, "ssh")[-1]
        self.assertNotIn("--session-token", anonymous)
        self.assertNotIn(RUNTIME_SESSION_TOKEN, anonymous)
        foreign = SSOT.RemoteConnectionProfile(
            "other-linux", "/srv/other/app", "/srv/other/ssot", 18765,
            WORKSPACE_ID, 8765, REQUIRED_REMOTE_PYTHON,
        )
        foreign_python = SSOT.RemoteConnectionProfile(
            "work-linux", "/srv/workstack/app", "/srv/workstack/ssot", 18765,
            WORKSPACE_ID, 8765, "/tmp/foreign-python",
        )
        self.assertEqual(
            SSOT.session_token_for_self_probe(owned, owned, RUNTIME_SESSION_TOKEN),
            RUNTIME_SESSION_TOKEN,
        )
        for label, active, target in (
            ("foreign-target", owned, foreign),
            ("foreign-python", owned, foreign_python),
            ("foreign-active", foreign, owned),
            ("no-active", None, owned),
        ):
            with self.subTest(case=label):
                self.assertIsNone(
                    SSOT.session_token_for_self_probe(active, target, RUNTIME_SESSION_TOKEN)
                )
        self.assertIsNone(SSOT.session_token_for_self_probe(owned, owned, None))
        self.assertIsNone(SSOT.session_token_for_self_probe(owned, owned, ""))

    def test_r6_generated_tokens_always_join_into_a_buildable_serve_command(self) -> None:
        """A hex token ending in `cd` meets the next flag as the forbidden `cd --`."""

        contract = sys.modules[SSOT.join_probe_command.__module__]
        profile = ssot_profile()
        with self.assertRaisesRegex(RuntimeError, "cd --"):
            SSOT.build_remote_server_command(profile, session_token="0" * 30 + "cd")
        for _attempt in range(2000):
            token = contract.generate_session_token()
            self.assertFalse(token.endswith("cd"), token)
        for _attempt in range(50):
            remote = SSOT.build_remote_server_command(
                profile, session_token=contract.generate_session_token()
            )
            self.assertEqual(forbidden_in(remote), ())
            self.assertEqual(bash_words(remote), csh_words(remote))

    def test_r2_stop_owned_command_is_login_shell_neutral(self) -> None:
        remote = SSOT.build_ssh_stop_owned_command(
            ssot_profile(), "ssh", RUNTIME_SESSION_TOKEN
        )[-1]
        self.assertEqual(forbidden_in(remote), ())
        self.assertIn(" stop-owned ", f" {remote} ")
        self.assertIn(RUNTIME_SESSION_TOKEN, remote)
        self.assertNotIn("pkill", remote)
        self.assertEqual(bash_words(remote), csh_words(remote))
        self.assertEqual(csh_words(remote), tcsh_words(remote))


class ShellNeutralRegressionTest(unittest.TestCase):
    def test_check_and_metadata_share_the_probe_entry_point(self) -> None:
        check = SSOT.build_ssh_check_command(ssot_profile(), "ssh")[-1]
        metadata = META.build_ssh_profile_metadata_command(metadata_profile(), "ssh.exe")[-1]
        self.assertEqual(check, metadata)

    def test_legacy_registry_without_remote_python_round_trips(self) -> None:
        payload = ssh_registry_profile()
        del payload["remote_python"]
        document = registry_document(payload)
        parsed = REGISTRY.registry_from_document(document)
        self.assertEqual(REGISTRY.registry_to_document(parsed), document)

    def test_new_remote_python_round_trips_byte_stable(self) -> None:
        document = registry_document(ssh_registry_profile())
        parsed = REGISTRY.registry_from_document(document)
        self.assertEqual(REGISTRY.registry_to_document(parsed), document)


if __name__ == "__main__":
    unittest.main()
