from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "windows"


class WindowsInstallerBundleContractTest(unittest.TestCase):
    def read(self, name: str) -> str:
        return (WINDOWS / name).read_text(encoding="utf-8-sig")

    def test_published_installer_bytes_are_not_normalized_by_git(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

        self.assertIn("installer/WorkStack-Setup-*.ps1 -text", attributes)

    def test_builder_pins_and_bundles_the_official_python_runtime(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("python-3.12.10-embed-amd64.zip", script)
        self.assertIn(
            "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
            script.lower(),
        )
        self.assertRegex(script, r"(?i)runtime\\python\.exe")
        self.assertIn("RuntimeArchivePath", script)
        self.assertIn("sys.path.insert(0, sys.argv[1])", script)

    def test_builder_emits_a_portable_sha256_sidecar(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn(".sha256", script)
        self.assertIn("UTF8Encoding", script)
        self.assertIn("GetFileName($output)", script)

    def test_one_file_setup_forwards_backup_policy_to_the_installer(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertGreaterEqual(script.count("[int]$BackupRetention = 14"), 1)
        self.assertGreaterEqual(script.count("[string]$BackupDir = ''"), 1)
        self.assertIn("@('DataDir', 'BackupDir', 'Port', 'BackupRetention')", script)
        self.assertIn("$PSBoundParameters.ContainsKey($optionalName)", script)
        self.assertIn("& $installer @installerArguments", script)

    def test_builder_removes_local_python_bytecode_before_packaging(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("function Remove-PythonBytecode", script)
        self.assertIn("'__pycache__'", script)
        self.assertIn("$_.Extension -in @('.pyc', '.pyo')", script)
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
        self.assertIn("runtime\\pythonw.exe", script)
        self.assertIn("desktop\\python-webview-shell\\workstack_desktop.py", script)
        self.assertIn(".IndexOf($desktopEntryPath, [StringComparison]::OrdinalIgnoreCase)", script)
        self.assertIn("$ownsServer -or $ownsDesktop", script)
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

    def test_installer_selects_an_available_loopback_port_after_stopping_an_upgrade(self) -> None:
        installer = self.read("Install-WorkStack.ps1")

        self.assertIn("function Resolve-AvailableLoopbackPort", installer)
        self.assertIn("$resolvedPort = Resolve-AvailableLoopbackPort -PreferredPort $Port", installer)
        self.assertIn("$configValues['port'] = $resolvedPort", installer)
        self.assertLess(
            installer.index("& $stopScript -InstallRoot $installPath"),
            installer.index("$resolvedPort = Resolve-AvailableLoopbackPort -PreferredPort $Port"),
        )

    def test_launcher_reports_a_non_workstack_port_collision_explicitly(self) -> None:
        start = self.read("Start-WorkStack.ps1")

        self.assertIn("Test-LoopbackPortListening", start)
        self.assertIn("is already in use by a non-Work Stack process", start)

    def test_offline_bundle_installs_every_requirement_with_hash_checking(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertNotIn("ExtractToDirectory", script)
        self.assertNotIn("baseWheelFiles", script)
        self.assertIn("--require-hashes", script)
        self.assertIn("requirements.txt", script)
        self.assertIn("requirements-windows-desktop.txt", script)
        self.assertIn("requirements-windows-build.txt", script)
        self.assertIn("setuptools.build_meta", script)
        self.assertIn("PYTHONPATH", script)
        build_requirements = (ROOT / "requirements-windows-build.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("setuptools==78.1.0", build_requirements)
        self.assertIn("--hash=sha256:", build_requirements)
        self.assertIn("import jsonschema", script)

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
        self.assertIn("$config.backup_retention", script)
        self.assertIn("$config.backup_dir", script)
        self.assertIn("-BackupDir $backupPath", script)
        self.assertIn("-NoShortcut:$NoShortcut", script)
        self.assertNotIn("SourceRoot", script)
        self.assertNotIn("Install-WorkStack.ps1", script)

    def test_installer_restores_configuration_when_upgrade_rolls_back(self) -> None:
        script = self.read("Install-WorkStack.ps1")

        self.assertIn("$originalConfigBytes", script)
        self.assertIn("function Write-Utf8NoBomAtomic", script)
        self.assertIn("function Restore-OriginalConfig", script)
        self.assertIn("Restore-OriginalConfig", script)
        self.assertIn("$preserveExistingConfig", script)
        self.assertIn("$existingConfig.PSObject.Properties", script)
        self.assertIn("$existingConfig.backup_dir", script)
        self.assertIn("$PSBoundParameters.ContainsKey('BackupDir')", script)
        self.assertIn("WORKSTACK_INSTALL_TEST_FAIL_AFTER_CONFIG_WRITE", script)

    def test_direct_reinstall_preserves_the_selected_ssot_path_by_default(self) -> None:
        script = self.read("Install-WorkStack.ps1")

        self.assertIn("$PSBoundParameters.ContainsKey('DataDir')", script)
        self.assertIn("$existingDataDir = [string]$existingConfig.data_dir", script)
        self.assertIn("$dataPath = [IO.Path]::GetFullPath($existingDataDir)", script)
        self.assertIn("$PSBoundParameters.ContainsKey('Port')", script)

    def test_shipping_upgrade_smoke_covers_105_preservation_and_rollback(self) -> None:
        script = self.read("Test-WorkStackUpgrade.ps1")

        self.assertIn("PreviousSetupPath", script)
        self.assertIn("CandidateSetupPath", script)
        self.assertIn("PreviousVersion", script)
        self.assertIn("1.0.5", script)
        self.assertIn("configuration bytes were not preserved", script.lower())
        self.assertIn("rollback did not restore the $previousversion payload", script.lower())
        self.assertIn("release-gate-marker.txt", script)
        self.assertIn("Set-IsReadOnly", script)
        self.assertIn("custom-backups", script)
        self.assertIn("custom backup directory", script.lower())
        self.assertIn("$global:LASTEXITCODE = 0", script)

    def test_post_install_launcher_failure_rolls_back_before_receipting_success(self) -> None:
        script = self.read("Apply-WorkStackUpdate.ps1")

        self.assertIn("function New-InstallRecoverySnapshot", script)
        self.assertIn("function Restore-InstallRecoverySnapshot", script)
        self.assertIn("WORKSTACK_UPDATE_TEST_FAIL_LAUNCHER_VALIDATION", script)
        self.assertIn("WORKSTACK_UPDATE_TEST_FAIL_RESTART", script)
        self.assertIn("-Status 'rolled-back'", script)
        self.assertIn("-Status 'recovery-required'", script)
        self.assertIn("recovery_path", script)
        self.assertLess(script.index("Start-Process"), script.index("-Status 'installed'"))
        self.assertIn("post-install launcher rollback", self.read("Test-WorkStackUpgrade.ps1").lower())

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

    def test_primary_shortcut_uses_the_signed_windowless_python_host_directly(self) -> None:
        installer = self.read("Install-WorkStack.ps1")
        launcher = self.read("Start-WorkStack.ps1")
        builder = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("runtime\\pythonw.exe", installer)
        self.assertIn("desktop\\python-webview-shell\\workstack_desktop.py", installer)
        self.assertNotIn("System32\\wscript.exe", installer)
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

        self.assertIn("function Write-Utf8NoBomAtomic", installer)
        self.assertIn("Write-Utf8NoBomAtomic -Path $configPath", installer)
        self.assertIn("Write-BytesAtomic -Path (Join-Path $installPath 'runtime-config.json')", installer)
        self.assertIn("[Text.UTF8Encoding]::new($false)", installer)

    def test_desktop_launcher_can_use_the_install_local_runtime_configuration(self) -> None:
        start = self.read("Start-WorkStack.ps1")

        self.assertIn("[string]$ConfigPath = ''", start)
        self.assertIn("[IO.Path]::GetFullPath($ConfigPath)", start)

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
