from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "windows"
SCRIPTS = ROOT / "scripts"

# Stands in for scripts/release_gate.py inside a synthetic source root. Its
# refresh-dist branch mocks only the npm build -- it reports the tree that is
# already there, using the real gate's own manifest and digest helpers -- and
# then applies the concurrent rewrite a packager cannot prevent. Its
# verify-staged-dist branch is not mocked at all: it delegates to the real
# release gate, so the packaging decision under test is the product's own.
STUB_RELEASE_GATE = '''
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(r"@SCRIPTS@")
ROOT = Path(__file__).resolve().parents[1]
RACE = ROOT / "race.json"
KEEP = ROOT / "race-original.bin"


def load(name):
    spec = importlib.util.spec_from_file_location("stub_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan():
    return json.loads(RACE.read_text(encoding="utf-8")) if RACE.is_file() else None


def apply_race():
    step = plan()
    if step is None:
        return
    target = ROOT / "frontend" / "dist" / step["path"]
    if target.is_file():
        KEEP.write_bytes(target.read_bytes())
    if step.get("content") is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(step["content"].encode("utf-8"))


def undo_race():
    step = plan()
    if step is None or not step.get("restore"):
        return
    target = ROOT / "frontend" / "dist" / step["path"]
    if KEEP.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(KEEP.read_bytes())
    elif target.is_file():
        target.unlink()


GATE = load("dist_source_gate")
if sys.argv[1] == "refresh-dist":
    files = GATE.dist_entries(ROOT / "frontend" / "dist")
    summary = {
        "schema_version": GATE.SCHEMA_VERSION,
        "dist_digest": GATE.dist_digest(files),
        "dist_file_count": len(files),
        "dist_files": files,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    sys.stdout.flush()
    apply_race()
    raise SystemExit(0)

undo_race()
RELEASE = load("release_gate")
try:
    raise SystemExit(RELEASE.main(sys.argv[1:]))
except RELEASE.ReleaseGateError as error:
    print("release gate failed: " + str(error), file=sys.stderr)
    raise SystemExit(1)
'''


def powershell() -> str | None:
    for candidate in ("powershell.exe", "pwsh.exe", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


class WindowsInstallerBundleContractTest(unittest.TestCase):
    def read(self, name: str) -> str:
        return (WINDOWS / name).read_text(encoding="utf-8-sig")

    def test_published_installer_bytes_are_not_normalized_by_git(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

        self.assertIn("installer/WorkStack-Setup-*.ps1 binary", attributes)

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

    def test_builder_binds_dist_to_its_source_before_copying_it(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("release_gate.py", script)
        self.assertIn("refresh-dist", script)
        self.assertIn("--repo $sourcePath", script)
        self.assertIn("$LASTEXITCODE -ne 0", script)
        self.assertIn("dist/source gate refused", script)
        self.assertLess(
            script.index("refresh-dist"),
            script.index("'frontend\\dist'"),
            "the gate must run before a single dist byte is copied",
        )

    def test_builder_binds_the_copied_dist_bytes_to_the_admitted_digest(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        self.assertIn("verify-staged-dist", script)
        self.assertIn("--expect $admittedDist.dist_digest", script)
        self.assertIn("--staged $stagedDist", script)
        self.assertIn("is not the tree the gate admitted", script)
        copy = script.index("Copy-Item -LiteralPath (Join-Path $sourcePath 'frontend")
        staged = script.index("verify-staged-dist")
        self.assertLess(script.index("refresh-dist"), copy)
        self.assertLess(copy, staged, "the copied bytes are what must be checked")
        self.assertLess(
            script.rindex("(Join-Path $sourcePath 'frontend"),
            staged,
            "the live dist must never be read again after the staged check",
        )

    def test_builder_never_judges_dist_freshness_by_timestamp(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")

        for forbidden in ("LastWriteTime", "CreationTime", "-NewerThan", "LastAccessTime"):
            self.assertNotIn(forbidden, script)

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

    def test_upgrade_backup_uses_the_smoke_tested_staged_runtime(self) -> None:
        installer = self.read("Install-WorkStack.ps1")

        self.assertIn("$stagedEntry = Join-Path $staging 'run_work_stack.py'", installer)
        self.assertIn("& $stagedPython $stagedEntry --data-dir $dataPath maintenance backup", installer)
        self.assertNotIn("& $installedPython $installedEntry --data-dir", installer)

    def test_stop_launcher_is_compatible_with_windows_powershell_51(self) -> None:
        script = self.read("Stop-WorkStack.ps1")

        self.assertIn(".IndexOf($entryPath, [StringComparison]::OrdinalIgnoreCase)", script)
        self.assertIn("runtime\\pythonw.exe", script)
        self.assertIn("desktop\\python-webview-shell\\workstack_desktop.py", script)
        # Desktop ownership is now decided by parsed argv positions rather than an
        # incidental substring, for both the branded host and the legacy pythonw.
        self.assertIn("Test-WorkStackDesktopInvocation", script)
        self.assertIn("-ExpectedEntry $desktopEntryPath", script)
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

    def test_successful_upgrade_retries_locked_rollback_cleanup_without_reverting_install(self) -> None:
        script = self.read("Install-WorkStack.ps1")

        self.assertIn("function Remove-DirectoryWithRetry", script)
        self.assertIn("[int]$Attempts = 20", script)
        self.assertIn("Start-Sleep -Milliseconds $DelayMilliseconds", script)
        self.assertIn("Work Stack was installed, but the previous runtime is still locked", script)
        self.assertNotIn(
            "if (Test-Path -LiteralPath $rollback) { Remove-Item -LiteralPath $rollback -Recurse -Force }",
            script,
        )

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
        """The behavioural check is unchanged; the link definitions moved.

        Install now delegates to the extracted shortcut helper, so the
        maintenance link is declared there rather than inline. The uninstaller
        still owns its own removal side.
        """

        installer = self.read("Install-WorkStack.ps1")
        helper = self.read("WorkStack-Shortcuts.ps1")
        uninstaller = self.read("Uninstall-WorkStack.ps1")

        self.assertIn("Invoke-WorkStackShortcutFinalization", installer)
        self.assertIn("Work Stack Maintenance.lnk", helper)
        self.assertIn("Maintain-WorkStack.ps1", helper)
        self.assertIn("Work Stack Maintenance.lnk", uninstaller)

    def test_uninstaller_only_removes_shortcuts_owned_by_its_install_root(self) -> None:
        uninstaller = self.read("Uninstall-WorkStack.ps1")

        self.assertIn("function Remove-OwnedShortcut", uninstaller)
        self.assertIn("$shortcut.TargetPath", uninstaller)
        self.assertIn("$shortcut.Arguments", uninstaller)
        self.assertIn("[StringComparison]::OrdinalIgnoreCase", uninstaller)
        # Both the branded host and the legacy launcher are recognised targets.
        self.assertIn("-ExpectedTargets $desktopTargets", uninstaller)
        self.assertIn("-ExpectedArgumentPath $desktopEntry", uninstaller)
        self.assertIn("-ExpectedArgumentPath $maintenanceScript", uninstaller)
        self.assertIn("Preserving shortcut whose Work Stack ownership could not be verified", uninstaller)
        self.assertNotIn(
            "if (Test-Path -LiteralPath $desktopShortcut) { Remove-Item",
            uninstaller,
        )

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

    def test_builder_retains_the_compiler_handle_and_fails_closed_on_unknown_exit(self) -> None:
        script = self.read("Build-WindowsInstaller.ps1")
        helper = self.read("Wait-WorkStackOwnedProcess.ps1")

        self.assertIn("$compile = Start-Process -FilePath $compiler", script)
        self.assertIn("$compile.Handle", script)
        self.assertLess(script.index("$compile = Start-Process"), script.index("$compile.Handle"))
        self.assertLess(script.index("$compile.Handle"), script.index("WaitForExit(30000)"))
        self.assertLess(script.index("$null -eq $compile.ExitCode"), script.index("$compile.ExitCode -ne 0"))
        self.assertLess(script.index("exit code is unknown"), script.index("was not produced"))
        self.assertIn("WaitForExit(30000)", script)
        self.assertIn("WaitForExit(5000)", script)
        self.assertNotRegex(script, r"WaitForExit\(\s*\)")
        self.assertIn("ConvertTo-WorkStackCommandLineArgument", script)
        self.assertEqual(1, script.count("Start-Process -FilePath $compiler"))
        self.assertIn("function Wait-WorkStackOwnedProcess", helper)
        self.assertIn("FUNCTIONS ONLY", helper)
        self.assertIn("TimeoutMilliseconds = 30000", helper)
        self.assertIn("KillConfirmMilliseconds = 5000", helper)
        self.assertNotRegex(helper, r"WaitForExit\(\s*\)")
        self.assertNotIn("csc.exe", helper)
        self.assertNotRegex(helper, r"(?m)^\s*Start-Process\b")

    def test_primary_shortcuts_use_the_packaged_product_icon(self) -> None:
        """The icon source changed; the behavioural check does not.

        Links still carry an IconLocation and the desktop folder is still
        resolved. What changed is that the icon is now the packaged versioned
        asset rather than a GDI-generated root file, and the link definitions
        live in the extracted helper.
        """

        installer = self.read("Install-WorkStack.ps1")
        helper = self.read("WorkStack-Shortcuts.ps1")

        # The obsolete generator and its root output are gone.
        self.assertNotIn("New-WorkStackIcon", installer)
        self.assertNotIn("WorkStack.ico", installer)
        self.assertNotIn("System.Drawing", installer)

        self.assertIn("IconLocation", helper)
        self.assertIn("GetFolderPath('Desktop')", helper)
        self.assertIn("WorkStack-Mark-Lime-v2.ico", helper)
        self.assertIn("'{0},0' -f", helper)

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


class WindowsDistStageConsumptionTest(unittest.TestCase):
    """The installer must ship the dist bytes the gate admitted, not a later tree.

    The marked dist stage of the real builder script is executed against a
    synthetic source root and a mocked release-gate command. Nothing is
    downloaded, no wheel or dependency is installed, no compiler runs and no
    installer is produced: only the gate call, the payload copy and the staged
    check that now stands between them and publication.
    """

    def setUp(self) -> None:
        self.shell = powershell()
        if self.shell is None:
            self.skipTest("no PowerShell host is available")
        self.temporary = tempfile.mkdtemp(prefix="workstack-dist-stage-")
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.base = Path(self.temporary)
        self.source = self.base / "source"
        self.payload = self.base / "payload"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "scripts" / "release_gate.py").write_text(
            STUB_RELEASE_GATE.replace("@SCRIPTS@", str(SCRIPTS)), encoding="utf-8"
        )
        self.dist = self.source / "frontend" / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_bytes(b"<html>admitted</html>\n")
        (self.dist / "assets" / "app.js").write_bytes(b"export const build = 1\n")
        self.payload.mkdir()

    def dist_stage(self) -> str:
        """The builder's own marked dist stage, verbatim."""

        script = (WINDOWS / "Build-WindowsInstaller.ps1").read_text(encoding="utf-8-sig")
        start = script.index("# ---- admitted frontend dist")
        end = script.index("# ---- end admitted frontend dist")
        return script[start:script.index("\n", end) + 1]

    def race(self, **step: object) -> None:
        (self.source / "race.json").write_text(json.dumps(step), encoding="utf-8")

    def run_stage(self) -> subprocess.CompletedProcess:
        quoted = lambda value: str(value).replace("'", "''")
        body = [
            "$ErrorActionPreference = 'Stop'",
            "$WorkStackTestPython = '" + quoted(sys.executable) + "'",
            "function python { & $WorkStackTestPython @args }",
            "$sourcePath = '" + quoted(self.source) + "'",
            "$payload = '" + quoted(self.payload) + "'",
            "New-Item -ItemType Directory -Force -Path (Join-Path $payload 'frontend') | Out-Null",
            self.dist_stage(),
        ]
        script = self.base / "stage.ps1"
        script.write_text("\n".join(body) + "\n", encoding="utf-8-sig")
        return subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def bytes_under(self, root: Path) -> dict:
        if not root.is_dir():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def staged(self) -> dict:
        return self.bytes_under(self.payload / "frontend" / "dist")

    def live(self) -> dict:
        return self.bytes_under(self.dist)

    def refuse_stage(self) -> str:
        completed = self.run_stage()
        output = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode, output)
        self.assertIn("DIST_STAGED_DRIFT", output)
        self.assertIn("was admitted", output)
        return output

    def test_an_undisturbed_dist_is_copied_and_accepted(self) -> None:
        admitted = self.live()

        completed = self.run_stage()

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(admitted, self.staged())
        self.assertIn("dist_digest", completed.stdout)

    def test_an_asset_rewritten_between_the_gate_and_the_copy_fails_packaging(self) -> None:
        """The live tree is honest again by the staged check, and it still refuses."""

        self.race(path="assets/app.js", content="export const build = 666\n", restore=True)
        admitted = self.live()

        self.refuse_stage()

        # The copy really did consume the rewritten bytes, so this is a verdict on
        # the payload rather than on the order the gate was called in -- and the
        # live tree matches the receipt again, so a second live check would have
        # let those bytes through.
        self.assertEqual(b"export const build = 666\n", self.staged()["assets/app.js"])
        self.assertEqual(admitted, self.live())

    def test_an_added_emitted_file_fails_packaging(self) -> None:
        self.race(path="assets/injected.js", content="export const injected = 1\n", restore=True)
        admitted = self.live()

        self.refuse_stage()

        self.assertIn("assets/injected.js", self.staged())
        self.assertEqual(admitted, self.live())

    def test_a_deleted_emitted_file_fails_packaging(self) -> None:
        self.race(path="assets/app.js", content=None, restore=True)
        admitted = self.live()

        self.refuse_stage()

        self.assertNotIn("assets/app.js", self.staged())
        self.assertEqual(admitted, self.live())


if __name__ == "__main__":
    unittest.main()
