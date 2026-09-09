"""Desktop stop callers must receive failure/unknown evidence from SSH.

The host seam delegates the argv and the ``subprocess.run`` to
``ssot_connection``; what it must keep is the shape these tests pin -- the
injected runner is honoured, a completed process comes back unread, and a
timeout is raised rather than turned into an answer.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

SHELL = Path(__file__).resolve().parents[1] / "desktop" / "python-webview-shell"
sys.path.insert(0, str(SHELL))
# Imported before the sys.modules patch below, which restores the whole dict
# on exit and would otherwise discard the instance the desktop bound to.
import ssot_connection as SSOT  # noqa: E402

SPEC = importlib.util.spec_from_file_location("desktop_stop_host_result", SHELL / "workstack_desktop.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)


class StopHostResultTests(unittest.TestCase):
    def request(self, host):
        with mock.patch.object(MODULE, "find_ssh_executable", return_value="ssh.exe"), \
             mock.patch.object(SSOT, "build_ssh_stop_owned_command", return_value=["ssh.exe", "fixture"]):
            return MODULE.WorkStackDesktopHost._request_remote_stop_owned(host, object(), "synthetic-token")

    def test_nonzero_remote_exit_is_returned_to_cleanup(self):
        result = subprocess.CompletedProcess(["ssh.exe"], 23, b"", b"refused")
        with mock.patch.object(SSOT.subprocess, "run", return_value=result):
            self.assertIs(self.request(types.SimpleNamespace(_stop_owned_runner=None)), result)

    def test_injected_runner_does_not_lose_stop_receipt(self):
        result = subprocess.CompletedProcess([], 0, b'{"status":"stopped"}', b"")
        self.assertIs(self.request(types.SimpleNamespace(_stop_owned_runner=lambda _: result)), result)

    def test_timeout_is_not_translated_into_success(self):
        with mock.patch.object(SSOT.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh.exe", 8)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.request(types.SimpleNamespace(_stop_owned_runner=None))


if __name__ == "__main__":
    unittest.main()
