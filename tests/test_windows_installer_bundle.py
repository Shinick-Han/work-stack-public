from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "windows"


class WindowsInstallerBundleContractTest(unittest.TestCase):
    def read(self, name: str) -> str:
        return (WINDOWS / name).read_text(encoding="utf-8-sig")

    def test_builder_pins_and_bundles_the_official_python_runtime(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("python-3.12.10-embed-amd64.zip", script)
        self.assertIn(
            "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
            script.lower(),
        )
        self.assertRegex(script, r"(?i)runtime\\python\.exe")
        self.assertIn("RuntimeArchivePath", script)

    def test_builder_emits_a_portable_sha256_sidecar(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn(".sha256", script)
        self.assertIn("UTF8Encoding", script)
        self.assertIn("GetFileName($output)", script)

    def test_builder_removes_local_python_bytecode_before_packaging(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("function Remove-PythonBytecode", script)
        self.assertIn("'__pycache__'", script)
        self.assertIn("'*.pyc', '*.pyo'", script)
        self.assertGreaterEqual(script.count("Remove-PythonBytecode -Root $payload"), 2)
        self.assertIn("$dependencyBin = Join-Path $sitePackages 'bin'", script)
        self.assertIn("Remove-Item -LiteralPath $dependencyBin -Recurse -Force", script)
        self.assertLess(
            script.rindex("Remove-PythonBytecode -Root $payload"),
            script.index("Compress-Archive"),
        )

    def test_setup_verifier_checks_exact_filename_and_digest(self) -> None:
        script = self.read("Test-WorkStackSetup.ps1")

        self.assertIn("[Parameter(Mandatory = $true)]", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("setup filename", script.lower())
        self.assertIn("hash mismatch", script.lower())

    def test_installer_never_discovers_or_creates_a_host_python_environment(self) -> None:
        script = self.read("Install-WorkStack.ps1")

        self.assertNotRegex(script, r"(?i)Get-Command\s+python")
        self.assertNotRegex(script, r"(?i)-m\s+venv")
        self.assertRegex(script, r"(?i)runtime\\python\.exe")
        self.assertIn("3.12:64", script)

    def test_all_installed_runtime_commands_use_the_bundled_interpreter(self) -> None:
        for name in ("Start-WorkStack.ps1", "Stop-WorkStack.ps1", "Maintain-WorkStack.ps1"):
            with self.subTest(name=name):
                script = self.read(name)
                self.assertRegex(script, r"(?i)runtime\\python\.exe")
                self.assertNotIn(".venv\\Scripts\\python.exe", script)

        installer = self.read("Install-WorkStack.ps1")
        self.assertRegex(installer, r"(?i)runtime\\python\.exe")
        self.assertIn("One-time compatibility", installer)

    def test_stop_launcher_is_compatible_with_windows_powershell_51(self) -> None:
        script = self.read("Stop-WorkStack.ps1")

        self.assertIn(".IndexOf($entryPath, [StringComparison]::OrdinalIgnoreCase)", script)
        self.assertNotIn(".Contains($entryPath, [StringComparison]::OrdinalIgnoreCase)", script)

    def test_installer_rejects_runtime_data_path_overlap_before_mutation(self) -> None:
        script = self.read("Install-WorkStack.ps1")

        self.assertIn("Assert-PathsDisjoint", script)
        self.assertIn("$installPath", script)
        self.assertIn("$statePath", script)
        self.assertIn("$dataPath", script)
        self.assertIn("$backupRoot", script)
        self.assertLess(script.index("Assert-PathsDisjoint"), script.index("New-Item -ItemType Directory -Force -Path $parent"))

    def test_upgrade_uses_the_staged_compatible_stopper(self) -> None:
        script = self.read("Install-WorkStack.ps1")

        self.assertIn("Join-Path $staging 'scripts\\windows\\Stop-WorkStack.ps1'", script)
        self.assertNotIn("Join-Path $installPath 'scripts\\windows\\Stop-WorkStack.ps1'", script)

    def test_launcher_reports_exact_server_ownership_and_quotes_paths(self) -> None:
        start = self.read("Start-WorkStack.ps1")
        stop = self.read("Stop-WorkStack.ps1")

        self.assertIn("$StatusPath", start)
        self.assertIn("Write-LaunchStatus", start)
        self.assertIn("ConvertTo-WindowsCommandLineArgument", start)
        self.assertIn("-Status 'started'", start)
        self.assertIn("-Status 'reused'", start)
        self.assertIn("[int]$ProcessId", stop)

    def test_offline_bundle_installs_every_requirement_with_hash_checking(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertNotIn("ExtractToDirectory", script)
        self.assertNotIn("baseWheelFiles", script)
        self.assertIn("--require-hashes", script)
        self.assertIn("requirements.txt", script)
        self.assertIn("requirements-windows-desktop.txt", script)

    def test_user_guide_does_not_require_python_or_node_on_the_target_machine(self) -> None:
        guide = (ROOT / "docs" / "WORKSTACK_WINDOWS_INSTALL_BACKUP_USER_GUIDE_2026-08-30.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("No Python or Node.js installation is required", guide)
        self.assertNotIn("Prerequisite: 64-bit Python 3.12", guide)
        self.assertNotIn(".venv\\Scripts\\python.exe", guide)

    def test_updater_requires_an_explicit_setup_artifact_and_preserves_configuration(self) -> None:
        script = self.read("Update-WorkStack.ps1")

        self.assertIn("[Parameter(Mandatory = $true)]", script)
        self.assertIn("$SetupPath", script)
        self.assertIn("$ChecksumPath", script)
        self.assertIn("Test-WorkStackSetup.ps1", script)
        self.assertLess(script.index("Test-WorkStackSetup.ps1"), script.index("& $setup"))
        self.assertIn("$config.data_dir", script)
        self.assertIn("$config.port", script)
        self.assertIn("-NoShortcut:$NoShortcut", script)
        self.assertNotIn("SourceRoot", script)
        self.assertNotIn("Install-WorkStack.ps1", script)

    def test_maintenance_launcher_is_offline_explicit_and_fail_closed(self) -> None:
        script = self.read("Maintain-WorkStack.ps1")

        self.assertIn("ValidateSet('Menu', 'Backup', 'Verify', 'Restore', 'Relocate')", script)
        self.assertIn("Get-CimInstance Win32_Process", script)
        self.assertIn("Work Stack must be stopped", script)
        self.assertIn("--replace", script)
        self.assertIn("--safety-backups", script)
        self.assertIn("source was preserved", script.lower())
        self.assertIn("ConvertTo-Json", script)
        self.assertIn("[Text.UTF8Encoding]::new($false)", script)
        self.assertIn("Move-Item -LiteralPath $temporaryConfig", script)

        verify_index = script.index("'maintenance', 'verify'")
        restore_index = script.index("'maintenance', 'restore'")
        self.assertLess(verify_index, restore_index)

    def test_installer_and_uninstaller_manage_the_maintenance_shortcut(self) -> None:
        installer = self.read("Install-WorkStack.ps1")
        uninstaller = self.read("Uninstall-WorkStack.ps1")

        self.assertIn("Work Stack Maintenance.lnk", installer)
        self.assertIn("Maintain-WorkStack.ps1", installer)
        self.assertIn("Work Stack Maintenance.lnk", uninstaller)

    def test_primary_shortcut_uses_the_signed_python_window_host(self) -> None:
        installer = self.read("Install-WorkStack.ps1")
        launcher = self.read("Start-WorkStack.ps1")
        builder = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("runtime\\pythonw.exe", installer)
        self.assertIn("desktop\\python-webview-shell\\workstack_desktop.py", installer)
        self.assertIn("requirements-windows-desktop.txt", builder)
        self.assertIn("desktop", builder)
        self.assertIn("requirements-windows-desktop.txt", installer)
        self.assertIn("Google\\Chrome\\Application\\chrome.exe", launcher)
        self.assertIn("Microsoft\\Edge\\Application\\msedge.exe", launcher)
        self.assertIn('"--app=$Url"', launcher)
        self.assertIn('"--user-data-dir=$ProfileRoot"', launcher)
        self.assertIn("browser-profile", launcher)
        self.assertGreaterEqual(launcher.count("Open-WorkStackBrowser -Url $url -ProfileRoot $browserProfilePath"), 2)

    def test_primary_shortcuts_use_the_generated_product_icon(self) -> None:
        installer = self.read("Install-WorkStack.ps1")

        self.assertIn("New-WorkStackIcon", installer)
        self.assertIn("WorkStack.ico", installer)
        self.assertIn("IconLocation", installer)
        self.assertIn("GetFolderPath('Desktop')", installer)
        self.assertIn("FromArgb(184, 242, 75)", installer)
        self.assertNotIn("FromArgb(174, 235, 61)", installer)

    def test_installer_writes_configuration_as_utf8_without_bom(self) -> None:
        installer = self.read("Install-WorkStack.ps1")

        self.assertIn("[IO.File]::WriteAllText((Join-Path $statePath 'config.json')", installer)
        self.assertIn("[Text.UTF8Encoding]::new($false)", installer)

    def test_remote_configurator_is_non_secret_strict_and_uses_a_distinct_forward(self) -> None:
        script = self.read("Configure-WorkStackRemote.ps1")

        self.assertIn("[int]$LocalForwardPort = 18765", script)
        self.assertIn("storage_mode = 'ssh-remote'", script)
        self.assertIn("workspace_id = $WorkspaceId.ToString().ToLowerInvariant()", script)
        self.assertIn("remote-connection.json", script)
        self.assertIn("--check-remote-connection", script)
        self.assertIn("[Text.UTF8Encoding]::new($false)", script)


if __name__ == "__main__":
    unittest.main()
