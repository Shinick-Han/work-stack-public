from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows" / "Test-WorkStackRemoteNetwork.ps1"


class SshNetworkDiagnosticsScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_diagnostic_is_read_only_against_the_remote_host(self) -> None:
        self.assertIn("--check-remote-connection", self.source)
        self.assertIn("StrictHostKeyChecking=yes", self.source)
        self.assertIn("BatchMode=yes", self.source)
        self.assertRegex(self.source, r"ssh_host_alias\) true")
        for mutating_remote_command in (" rm ", " mv ", " cp ", "mkdir --", "chmod ", "chown "):
            self.assertNotIn(mutating_remote_command, self.source)

    def test_receipt_omits_effective_hostname_user_and_key_paths(self) -> None:
        receipt = self.source[self.source.index("$receipt ="):]
        self.assertNotIn("effective.hostname", receipt)
        self.assertNotIn("effective.user", receipt)
        self.assertNotIn("identityfile", receipt.casefold())
        self.assertIn("proxy_jump_configured", receipt)
        self.assertIn("median_milliseconds", receipt)


if __name__ == "__main__":
    unittest.main()
