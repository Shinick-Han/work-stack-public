from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "ssh_config_discovery.py"
SPEC = importlib.util.spec_from_file_location("ssh_config_discovery_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SshConfigDiscoveryTest(unittest.TestCase):
    def test_discovers_only_concrete_safe_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.write_text(
                """
                Host work-linux build_2 WORK-LINUX
                  HostName 10.0.0.2
                  IdentityFile ~/.ssh/private-key
                Host * !blocked *.internal unsafe;command -option
                Host dev?.example [abc]
                Host quoted # ordinary comment
                """,
                encoding="utf-8",
            )

            self.assertEqual(
                MODULE.discover_ssh_host_aliases(config),
                ("build_2", "quoted", "work-linux"),
            )

    def test_follows_bounded_includes_and_ignores_cycles_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            snippets = root / "conf.d"
            snippets.mkdir()
            config = root / "config"
            outside_config = Path(outside) / "outside.conf"
            config.write_text(
                f'Include conf.d/*.conf\nInclude "{outside_config.as_posix()}"\nHost root-host\n',
                encoding="utf-8",
            )
            (snippets / "one.conf").write_text(
                "Include conf.d/two.conf\nHost included-one\n", encoding="utf-8"
            )
            (snippets / "two.conf").write_text(
                "Include conf.d/one.conf\nHost included-two\n", encoding="utf-8"
            )
            outside_config.write_text("Host escaped\n", encoding="utf-8")

            self.assertEqual(
                MODULE.discover_ssh_host_aliases(config),
                ("included-one", "included-two", "root-host"),
            )

    def test_supports_openssh_equals_directive_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "included").write_text("Host=included-host\n", encoding="utf-8")
            config = root / "config"
            config.write_text("Include=included\nHost=root-host\n", encoding="utf-8")

            self.assertEqual(
                MODULE.discover_ssh_host_aliases(config),
                ("included-host", "root-host"),
            )

    def test_zero_include_depth_reads_only_root_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "child").write_text("Host child-host\n", encoding="utf-8")
            config = root / "config"
            config.write_text("Include child\nHost root-host\n", encoding="utf-8")

            self.assertEqual(
                MODULE.discover_ssh_host_aliases(config, max_include_depth=0),
                ("root-host",),
            )

    @mock.patch.object(MODULE.subprocess, "run")
    def test_resolves_with_fixed_read_only_command_and_retains_no_key_data(
        self, run: mock.Mock
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            "host work-linux\nhostname server.example\nuser builder\nport 2222\n"
            "identityfile C:/secret/id_ed25519\nproxycommand secret command\n",
            "",
        )

        resolved = MODULE.resolve_ssh_host("work-linux", ssh_executable="ssh.exe")

        self.assertEqual(
            resolved,
            MODULE.ResolvedSshHost("work-linux", "server.example", "builder", 2222),
        )
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "ssh.exe",
                "-G",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "--",
                "work-linux",
            ],
        )
        self.assertNotIn("secret", repr(resolved))
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertFalse(run.call_args.kwargs["check"])

    @mock.patch.object(MODULE.subprocess, "run")
    def test_rejects_argument_injection_before_starting_openssh(self, run: mock.Mock) -> None:
        unsafe = ("-oProxyCommand=calc", "work host", "work;calc", "*.internal", "!blocked")
        for alias in unsafe:
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                MODULE.resolve_ssh_host(alias, ssh_executable="ssh.exe")
        run.assert_not_called()

    @mock.patch.object(MODULE.subprocess, "run")
    def test_failure_does_not_echo_openssh_output(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 255, "", "sensitive diagnostic")

        with self.assertRaisesRegex(RuntimeError, "could not resolve") as raised:
            MODULE.resolve_ssh_host("work-linux", ssh_executable="ssh.exe")

        self.assertNotIn("sensitive", str(raised.exception))

    @mock.patch.object(MODULE.subprocess, "run")
    def test_rejects_incomplete_or_invalid_resolution(self, run: mock.Mock) -> None:
        cases = (
            "hostname server\nuser builder\n",
            "hostname server\nuser builder\nport 70000\n",
            "hostname server\nuser builder\nport not-a-port\n",
        )
        for output in cases:
            with self.subTest(output=output):
                run.return_value = subprocess.CompletedProcess([], 0, output, "")
                with self.assertRaises(RuntimeError):
                    MODULE.resolve_ssh_host("work-linux", ssh_executable="ssh.exe")


if __name__ == "__main__":
    unittest.main()
