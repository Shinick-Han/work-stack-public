"""Contract for deferred Work Stack shortcut finalization.

Two kinds of assertion, and the second is the load-bearing one.

* Static AST assertions read the real parsed PowerShell of the shipped scripts:
  where the commit boundary sits, what the rollback-eligible scope contains, and
  that the helper is functions only. They supplement, they do not replace.
* Effect tests EXECUTE the real parsed extents of the shipped functions with
  every external effect substituted. COM, the native notifier, the shell folders
  and the filesystem probes are injected; nothing real is touched. A missing
  substitute refuses rather than falling through to a real installer.

``WORKSTACK_SHORTCUT_SOURCE_ROOT`` points the identical assertions at a
contained archive of the fixed base, so the same suite records RED there and
GREEN at the candidate. It defaults to this checkout, so no production seam
exists.

Never executed here: the installer or updater top level, real COM, real native
calls, real link creation and any live Shell action.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _source_root() -> Path:
    """Where the PowerShell under inspection lives; overridable for RED runs."""

    configured = os.environ.get("WORKSTACK_SHORTCUT_SOURCE_ROOT")
    return Path(configured).resolve() if configured else _REPOSITORY_ROOT


def _windows_script(name: str) -> Path:
    return _source_root() / "scripts" / "windows" / name


INSTALL = "Install-WorkStack.ps1"
APPLY = "Apply-WorkStackUpdate.ps1"
HELPER = "WorkStack-Shortcuts.ps1"

ICON_RELATIVE = r"desktop\python-webview-shell\assets\WorkStack-Mark-Lime-v2.ico"


def _powershell() -> str:
    for candidate in ("powershell.exe", "pwsh.exe"):
        located = __import__("shutil").which(candidate)
        if located:
            return located
    raise unittest.SkipTest("no PowerShell host is available")


def _run_powershell(script: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a bounded PowerShell snippet in a contained temporary directory.

    The snippet is written to a file so quoting cannot corrupt it. The child
    inherits this process's contained environment.
    """

    with tempfile.TemporaryDirectory() as scratch:
        script_path = Path(scratch) / "probe.ps1"
        script_path.write_text(script, encoding="utf-8-sig")
        return subprocess.run(
            [
                _powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=scratch,
        )


def _text(value: object) -> str:
    """Normalize a probe string field.

    Windows PowerShell 5.1's ConvertTo-Json can wrap a string as
    ``{"value": ..., "Length": n}``. Accepting both shapes keeps the same
    assertions working on either host.
    """

    if isinstance(value, dict) and "value" in value:
        return str(value["value"])
    return str(value)


def _parse_json_result(completed: subprocess.CompletedProcess) -> dict:
    """The probe's last stdout line is its JSON result; anything else is a fault."""

    if completed.returncode != 0:
        raise AssertionError(
            "probe failed (exit {}):\n{}\n{}".format(
                completed.returncode, completed.stdout, completed.stderr
            )
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("probe produced no result:\n" + completed.stderr)
    return json.loads(lines[-1])


# --------------------------------------------------------------------------- #
# Static AST facts about the shipped PowerShell.
# --------------------------------------------------------------------------- #

_AST_PROBE = r"""
$ErrorActionPreference = 'Stop'
$root = $env:WORKSTACK_PROBE_SOURCE_ROOT
function Get-Ast([string]$Path) {
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$errors)
    if ($errors.Count -gt 0) { throw "parse errors in ${Path}: $($errors.Count)" }
    return $ast
}
$installPath = Join-Path $root 'scripts\windows\Install-WorkStack.ps1'
$applyPath = Join-Path $root 'scripts\windows\Apply-WorkStackUpdate.ps1'
$helperPath = Join-Path $root 'scripts\windows\WorkStack-Shortcuts.ps1'

$result = [ordered]@{}
$result.helper_exists = Test-Path -LiteralPath $helperPath -PathType Leaf
$result.install_text = (Get-Content -LiteralPath $installPath -Raw)
$result.apply_text = (Get-Content -LiteralPath $applyPath -Raw)

$applyAst = Get-Ast $applyPath
# The rollback-eligible scope is the try whose catch calls Restore.
$tryStatements = $applyAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.TryStatementAst] }, $true)
$rollbackTry = $tryStatements | Where-Object {
    $_.CatchClauses.Extent.Text -join "`n" -match 'Restore-InstallRecoverySnapshot'
} | Select-Object -First 1
if (-not $rollbackTry) { throw 'no rollback-eligible try/catch found in Apply' }
$result.rollback_try_start = $rollbackTry.Extent.StartOffset
$result.rollback_try_end = $rollbackTry.Extent.EndOffset
$result.rollback_body = $rollbackTry.Body.Extent.Text

# The acceptance call is the WaitForExit(1500) guard.
$waitFor = $applyAst.FindAll({
    param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $n.Member.Value -eq 'WaitForExit'
}, $true) | Select-Object -First 1
if (-not $waitFor) { throw 'no WaitForExit acceptance found in Apply' }
$result.waitforexit_offset = $waitFor.Extent.StartOffset
$result.waitforexit_text = $waitFor.Extent.Text

# Where finalization and the receipt are invoked, by real command AST.
foreach ($pair in @(
    @{ key = 'finalize_offsets'; name = 'Invoke-WorkStackShortcutFinalization'; ast = $applyAst },
    @{ key = 'receipt_offsets'; name = 'Write-UpdateReceipt'; ast = $applyAst },
    @{ key = 'restore_offsets'; name = 'Restore-InstallRecoverySnapshot'; ast = $applyAst }
)) {
    $offsets = @($pair.ast.FindAll({
        param($n) $n -is [System.Management.Automation.Language.CommandAst]
    }, $true) | Where-Object { $_.GetCommandName() -eq $pair.name } | ForEach-Object { $_.Extent.StartOffset })
    $result[$pair.key] = $offsets
}

# Updater invocation and the NoShortcut argument it passes.
$updaterCall = $applyAst.FindAll({
    param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.Extent.Text -match '\$updater'
}, $true) | Select-Object -First 1
$result.updater_call = if ($updaterCall) { $updaterCall.Extent.Text } else { '' }

$installAst = Get-Ast $installPath
$result.install_functions = @($installAst.FindAll({
    param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true) | ForEach-Object { $_.Name })
$result.install_finalize = @($installAst.FindAll({
    param($n) $n -is [System.Management.Automation.Language.CommandAst]
}, $true) | Where-Object { $_.GetCommandName() -eq 'Invoke-WorkStackShortcutFinalization' }).Count

if ($result.helper_exists) {
    $helperAst = Get-Ast $helperPath
    $result.helper_top_level = @($helperAst.EndBlock.Statements | ForEach-Object { $_.GetType().Name })
    $result.helper_functions = @($helperAst.FindAll({
        param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
    }, $true) | ForEach-Object { $_.Name })
    $result.helper_text = (Get-Content -LiteralPath $helperPath -Raw)
} else {
    $result.helper_top_level = @()
    $result.helper_functions = @()
    $result.helper_text = ''
}
$result | ConvertTo-Json -Depth 6 -Compress
"""


class ParsedSourceFacts(unittest.TestCase):
    """Static facts read from the real parsed PowerShell."""

    @classmethod
    def setUpClass(cls) -> None:
        environment = dict(os.environ)
        environment["WORKSTACK_PROBE_SOURCE_ROOT"] = str(_source_root())
        with tempfile.TemporaryDirectory() as scratch:
            script_path = Path(scratch) / "ast.ps1"
            script_path.write_text(_AST_PROBE, encoding="utf-8-sig")
            completed = subprocess.run(
                [
                    _powershell(), "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                ],
                capture_output=True, text=True, timeout=180,
                cwd=scratch, env=environment,
            )
        cls.facts = _parse_json_result(completed)

    # -- helper shape ------------------------------------------------------
    def test_helper_exists_and_is_functions_only(self) -> None:
        self.assertTrue(self.facts["helper_exists"], HELPER + " is missing")
        top_level = self.facts["helper_top_level"]
        self.assertTrue(top_level, "the helper parsed to nothing")
        self.assertEqual(
            sorted(set(top_level)), ["FunctionDefinitionAst"],
            "the helper must contain only function definitions; found " + repr(top_level),
        )

    def test_helper_exposes_the_finalizer_and_shared_guard(self) -> None:
        functions = self.facts["helper_functions"]
        self.assertIn("Invoke-WorkStackShortcutFinalization", functions)
        self.assertIn("Assert-WorkStackShortcutInstallPath", functions)

    def test_helper_defers_native_type_creation(self) -> None:
        """Add-Type must sit inside a function, never at load."""

        text = _text(self.facts["helper_text"])
        self.assertIn("Add-Type", text)
        for statement_type in self.facts["helper_top_level"]:
            self.assertEqual(statement_type, "FunctionDefinitionAst")

    def test_helper_declares_the_reviewed_notification_seam(self) -> None:
        text = _text(self.facts["helper_text"])
        self.assertIn("shell32.dll", text)
        self.assertIn("SHChangeNotify", text)
        self.assertIn("ExactSpelling = true", text)
        self.assertIn("UnmanagedType.LPWStr", text)
        self.assertIn("CharSet.Unicode", text)
        self.assertIn("public static extern void SHChangeNotify", text)
        # Check what the helper DOES, not what its prose says it avoids: the
        # comments documenting these exclusions are exactly where the words
        # appear. Both block comments and line comments are removed first.
        code = __import__("re").sub(r"<#.*?#>", "", text, flags=__import__("re").S)
        code = "\n".join(
            line for line in code.splitlines() if not line.lstrip().startswith("#")
        )
        for forbidden in ("SHCNE_ASSOCCHANGED", "0x08000000", "Stop-Process", "Start-Process"):
            self.assertNotIn(forbidden, code, "helper must not use " + forbidden)

    # -- the commit boundary ----------------------------------------------
    def test_finalization_is_invoked_after_the_acceptance_call(self) -> None:
        finalize = self.facts["finalize_offsets"]
        self.assertTrue(finalize, "Apply never calls the finalizer")
        self.assertTrue(
            min(finalize) > self.facts["waitforexit_offset"],
            "finalization must follow the 1.5s acceptance",
        )

    def test_finalization_is_outside_the_rollback_eligible_scope(self) -> None:
        """The load-bearing structural fact: a helper call nested in the old try
        would let a derived failure roll back an accepted runtime."""

        for offset in self.facts["finalize_offsets"]:
            self.assertGreater(
                offset, self.facts["rollback_try_end"],
                "finalization must not sit inside the rollback-eligible try",
            )

    def test_no_restore_is_reachable_after_the_boundary(self) -> None:
        for offset in self.facts["restore_offsets"]:
            self.assertLess(
                offset, self.facts["rollback_try_end"],
                "Restore must not be reachable after the commit boundary",
            )

    def test_the_success_receipt_is_written_outside_the_rollback_scope(self) -> None:
        receipts = self.facts["receipt_offsets"]
        self.assertTrue(
            any(offset > self.facts["rollback_try_end"] for offset in receipts),
            "the committed receipt must be written after the rollback scope closes",
        )

    def test_rollback_body_no_longer_contains_the_success_reporting(self) -> None:
        body = self.facts["rollback_body"]
        self.assertIn("WaitForExit", body, "acceptance still belongs inside the try")
        self.assertNotIn("Invoke-WorkStackShortcutFinalization", body)
        self.assertNotIn("Shortcuts incomplete", body)

    # -- suppression and policy -------------------------------------------
    def test_apply_forces_suppression_on_the_transactional_install(self) -> None:
        self.assertRegex(
            self.facts["updater_call"], r"-NoShortcut:\$true",
            "the transactional install must be told to write no links",
        )

    def test_apply_validates_the_install_path_with_the_original_intent(self) -> None:
        text = _text(self.facts["apply_text"])
        self.assertIn("Assert-WorkStackShortcutInstallPath", text)
        self.assertIn("$originalNoShortcut = [bool]$NoShortcut", text)
        self.assertRegex(text, r"OriginalNoShortcut \$originalNoShortcut")

    def test_apply_reports_applied_with_warning_without_a_new_status(self) -> None:
        text = _text(self.facts["apply_text"])
        self.assertIn("Shortcuts incomplete: ", text)
        self.assertIn("Write-UpdateReceipt -Status 'installed'", text)
        for invented in ("'shortcuts-incomplete'", "'installed-with-warning'", "'partial'"):
            self.assertNotIn(invented, text, "no new receipt status may be invented")

    def test_apply_keeps_the_existing_receipt_schema_and_cap(self) -> None:
        text = _text(self.facts["apply_text"])
        self.assertIn("schema_version = 1", text)
        self.assertIn("[Math]::Min($Message.Length, 500)", text)

    # -- installer icon and link stage ------------------------------------
    def test_the_gdi_generator_and_root_icon_are_gone(self) -> None:
        text = _text(self.facts["install_text"])
        self.assertNotIn("New-WorkStackIcon", text)
        self.assertNotIn("WorkStack.ico", text)
        self.assertNotIn("System.Drawing", text)
        self.assertNotIn("New-WorkStackIcon", self.facts["install_functions"])

    def test_install_delegates_to_the_shared_finalizer(self) -> None:
        self.assertEqual(
            self.facts["install_finalize"], 1,
            "Install must finalize through the shared helper exactly once",
        )

    def test_install_validates_the_packaged_icon_before_destructive_effects(self) -> None:
        text = _text(self.facts["install_text"])
        self.assertIn(
            "Assert-WorkStackShortcutIconAsset", text,
            "Install must validate the packaged icon leaf",
        )
        self.assertIn("New-Item -ItemType Directory -Path $staging", text)
        self.assertLess(
            text.index("Assert-WorkStackShortcutIconAsset"),
            text.index("New-Item -ItemType Directory -Path $staging"),
            "the icon check must precede staging",
        )

    def test_install_uses_the_shared_path_policy_with_its_own_switch(self) -> None:
        text = _text(self.facts["install_text"])
        self.assertIn("Assert-WorkStackShortcutInstallPath", text)
        self.assertIn("only writes under LOCALAPPDATA\\Programs", _text(self.facts["helper_text"]))


# --------------------------------------------------------------------------- #
# Executed effects: the real parsed helper functions, every effect substituted.
# --------------------------------------------------------------------------- #

_HELPER_PREFLIGHT = r"""
# Parse the captured helper bytes before evaluating any of its definitions.
# Returning exact function extents also excludes script directives or preambles
# from evaluation; a changed file cannot race a second dot-source read.
function Get-WorkStackTestHelperDefinitions([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'REFUSE: helper is missing'
    }
    $text = [IO.File]::ReadAllText($Path)
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$errors)
    if ($errors.Count) { throw 'REFUSE: helper parse failed' }
    if ($ast.ParamBlock -or $ast.BeginBlock -or $ast.ProcessBlock -or $ast.UsingStatements.Count -or $ast.ScriptRequirements) {
        throw 'REFUSE: helper must contain function definitions only'
    }
    $statements = @($ast.EndBlock.Statements)
    $required = @(
        'Get-WorkStackShortcutNotificationConstant', 'Assert-WorkStackShortcutInstallPath',
        'Get-WorkStackShortcutIconPath', 'Assert-WorkStackShortcutIconAsset',
        'Get-WorkStackManagedShortcut', 'New-WorkStackShellChangeNotifier',
        'Send-WorkStackShortcutNotification', 'Invoke-WorkStackShortcutFinalization',
        'ConvertTo-WorkStackCommandLineArgument'
    )
    if ($statements.Count -ne $required.Count -or
        @($statements | Where-Object { $_ -isnot [Management.Automation.Language.FunctionDefinitionAst] }).Count) {
        throw 'REFUSE: helper must contain function definitions only'
    }
    foreach ($name in $required) {
        if (@($statements | Where-Object { $_.Name -ceq $name }).Count -ne 1) {
            throw "REFUSE: required helper function is missing: $name"
        }
    }
    return ($statements.Extent.Text -join "`n")
}

# These rejecting leaves exist before any helper evaluation. The Apply fixture
# later replaces Start-Process with its explicit recorder, never the cmdlet.
function Add-Type { throw 'REFUSE: real Add-Type is forbidden in this fixture' }
function Start-Process { throw 'REFUSE: real process launch is forbidden in this fixture' }
function Stop-Process { throw 'REFUSE: real process termination is forbidden in this fixture' }
function New-Object {
    param([string]$TypeName, [string]$ComObject)
    if ($ComObject) { throw 'REFUSE: real COM is forbidden in this fixture' }
    Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName
}

function Invoke-WorkStackTestFinalization {
    param($InstallPath, $StatePath, $StartMenuPath, $DesktopPath,
          $ShortcutFactory, $Notifier, $ExistenceProbe)
    foreach ($name in @('InstallPath', 'StatePath', 'StartMenuPath', 'DesktopPath')) {
        if (-not $PSBoundParameters[$name]) { throw "REFUSE: missing path substitute: $name" }
    }
    foreach ($name in @('ShortcutFactory', 'Notifier', 'ExistenceProbe')) {
        if ($PSBoundParameters[$name] -isnot [scriptblock]) {
            throw "REFUSE: missing effect substitute: $name"
        }
    }
    Invoke-WorkStackShortcutFinalization @PSBoundParameters
}

function Invoke-WorkStackTestNotification {
    param($Path, $Existed, $Notifier)
    if ($Notifier -isnot [scriptblock]) { throw 'REFUSE: missing effect substitute: Notifier' }
    Send-WorkStackShortcutNotification @PSBoundParameters
}
"""


_EFFECT_PREAMBLE = _HELPER_PREFLIGHT + r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$root = $env:WORKSTACK_PROBE_SOURCE_ROOT
$helper = Join-Path $root 'scripts\windows\WorkStack-Shortcuts.ps1'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
    throw "REFUSE: the helper under test is missing: $helper"
}
$helperDefinitions = Get-WorkStackTestHelperDefinitions $helper
. ([scriptblock]::Create($helperDefinitions))
function New-WorkStackShellChangeNotifier { throw 'REFUSE: real native notifier is forbidden in this fixture' }
foreach ($required in @(
    'Invoke-WorkStackShortcutFinalization',
    'Get-WorkStackManagedShortcut',
    'Send-WorkStackShortcutNotification',
    'Assert-WorkStackShortcutInstallPath'
)) {
    if (-not (Get-Command -Name $required -CommandType Function -ErrorAction SilentlyContinue)) {
        throw "REFUSE: required function is missing: $required"
    }
}

# Substitutes. Every one must be bound before the extents run; nothing below
# touches COM, the native API, the real Shell or the real filesystem.
$script:Saved = New-Object System.Collections.ArrayList
$script:Notified = New-Object System.Collections.ArrayList
$script:Existing = @{}
$script:FailSaveAt = -1

function New-FakeShortcut([string]$Path) {
    $state = [pscustomobject]@{
        Path = $Path; TargetPath = ''; Arguments = ''; WorkingDirectory = ''
        IconLocation = ''; IconAtSave = ''
    }
    $state | Add-Member -MemberType ScriptMethod -Name Save -Value {
        if ($script:FailSaveAt -ge 0 -and $script:Saved.Count -eq $script:FailSaveAt) {
            throw 'Injected shortcut save failure.'
        }
        $this.IconAtSave = $this.IconLocation
        [void]$script:Saved.Add([pscustomobject]@{
            Path = $this.Path; TargetPath = $this.TargetPath; Arguments = $this.Arguments
            WorkingDirectory = $this.WorkingDirectory; IconLocation = $this.IconAtSave
        })
    }
    return $state
}
$shortcutFactory = { param($Path) New-FakeShortcut $Path }
$notifier = { param($EventId, $Flags, $Path)
    [void]$script:Notified.Add([pscustomobject]@{ EventId = $EventId; Flags = $Flags; Path = $Path })
}
$existenceProbe = { param($Path) [bool]$script:Existing[$Path] }
"""


class SubstitutedFinalizationEffects(unittest.TestCase):
    """Execute the real helper functions with every effect substituted."""

    def run_probe(self, body: str) -> dict:
        environment = dict(os.environ)
        environment["WORKSTACK_PROBE_SOURCE_ROOT"] = str(_source_root())
        with tempfile.TemporaryDirectory() as scratch:
            install_root = Path(scratch) / "install"
            (install_root / "desktop" / "python-webview-shell" / "assets").mkdir(parents=True)
            (install_root / ICON_RELATIVE.replace("\\", "/")).write_bytes(b"icon")
            state_root = Path(scratch) / "state"
            state_root.mkdir()
            script_path = Path(scratch) / "effect.ps1"
            script_path.write_text(
                _EFFECT_PREAMBLE
                + "\n$installRoot = '{}'\n$stateRoot = '{}'\n".format(
                    str(install_root).replace("'", "''"), str(state_root).replace("'", "''")
                )
                + "$startMenu = '{}'\n$desktop = '{}'\n".format(
                    str(Path(scratch) / "StartMenu").replace("'", "''"),
                    str(Path(scratch) / "Desktop").replace("'", "''"),
                )
                + body,
                encoding="utf-8-sig",
            )
            completed = subprocess.run(
                [
                    _powershell(), "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                ],
                capture_output=True, text=True, timeout=180,
                cwd=scratch, env=environment,
            )
        return _parse_json_result(completed)

    SUCCESS_BODY = r"""
$result = Invoke-WorkStackTestFinalization -InstallPath $installRoot -StatePath $stateRoot `
    -StartMenuPath $startMenu -DesktopPath $desktop `
    -ShortcutFactory $shortcutFactory -Notifier $notifier -ExistenceProbe $existenceProbe
[ordered]@{
    saved = @($script:Saved)
    notified = @($script:Notified)
    complete = $result.Complete
    incomplete = $result.IncompleteReason
    install_root = $installRoot; state_root = $stateRoot
    start_menu = $startMenu; desktop = $desktop
} | ConvertTo-Json -Depth 6 -Compress
"""

    def assert_exact_descriptors(self, facts: dict) -> None:
        install = Path(_text(facts["install_root"]))
        state = _text(facts["state_root"])
        start_menu = Path(_text(facts["start_menu"]))
        desktop = Path(_text(facts["desktop"]))
        entry = install / "desktop/python-webview-shell/workstack_desktop.py"
        icon = str(install / ICON_RELATIVE.replace("\\", "/")) + ",0"
        arguments = f'"{entry}" --install-root "{install}" --state-root "{state}"'
        application = {
            "TargetPath": str(install / "WorkStack.exe"),
            "Arguments": arguments, "WorkingDirectory": str(install), "IconLocation": icon,
        }
        maintenance = install / "scripts/windows/Maintain-WorkStack.ps1"
        expected = [
            {"Path": str(start_menu / "Work Stack.lnk"), **application},
            {"Path": str(desktop / "Work Stack.lnk"), **application},
            {"Path": str(start_menu / "Work Stack Maintenance.lnk"),
             "TargetPath": "powershell.exe",
             "Arguments": f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{maintenance}" -InstallRoot "{install}" -StateRoot "{state}"',
             "WorkingDirectory": str(install), "IconLocation": icon},
        ]
        # Saved captures every field inside Save; this comparison therefore also
        # rejects late assignment, extra fields and any changed descriptor order.
        self.assertEqual(facts["saved"], expected)

    def test_exactly_three_links_with_frozen_targets_and_versioned_icons(self) -> None:
        self.assert_exact_descriptors(self.run_probe(self.SUCCESS_BODY))

    def test_icon_is_set_before_save_for_every_link(self) -> None:
        """IconAtSave is captured inside Save, so a late assignment would fail."""

        facts = self.run_probe(self.SUCCESS_BODY)
        for entry in facts["saved"]:
            self.assertTrue(entry["IconLocation"], "icon was not set before Save")

    def test_notifications_follow_the_saves_with_the_reviewed_constants(self) -> None:
        facts = self.run_probe(self.SUCCESS_BODY)
        notified = facts["notified"]
        self.assertEqual(len(notified), 3)
        self.assertEqual([entry["Path"] for entry in notified],
                         [entry["Path"] for entry in facts["saved"]])
        for entry in notified:
            self.assertEqual(entry["EventId"], 0x00000002)  # absent -> SHCNE_CREATE
            self.assertEqual(entry["Flags"], 0x0005 | 0x2000)
        self.assertTrue(facts["complete"])

    def test_an_existing_link_is_announced_as_an_update(self) -> None:
        body = (
            "$script:Existing[(Join-Path $startMenu 'Work Stack.lnk')] = $true\n"
            + self.SUCCESS_BODY
        )
        facts = self.run_probe(body)
        by_path = {entry["Path"]: entry for entry in facts["notified"]}
        existing = [value for key, value in by_path.items() if key.endswith(r"StartMenu\Work Stack.lnk")]
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0]["EventId"], 0x00002000)  # SHCNE_UPDATEITEM

    def test_unicode_and_spaced_paths_are_carried_verbatim(self) -> None:
        body = r"""
$startMenu = Join-Path $startMenu 'Пуск меню'
$desktop = Join-Path $desktop 'Рабочий стол'
""" + self.SUCCESS_BODY
        facts = self.run_probe(body)
        self.assert_exact_descriptors(facts)
        self.assertEqual(len(facts["saved"]), 3)
        self.assertTrue(any("Пуск меню" in entry["Path"] for entry in facts["saved"]))
        self.assertTrue(any("Рабочий стол" in entry["Path"] for entry in facts["saved"]))

    def test_an_over_length_path_is_reported_unsupported_not_refreshed(self) -> None:
        body = r"""
$long = 'x' * 300
$outcome = Invoke-WorkStackTestNotification -Path $long -Existed $false -Notifier $notifier
[ordered]@{
    saved = @()
    notified = @($script:Notified)
    complete = $outcome.Notified
    incomplete = $outcome.Reason
} | ConvertTo-Json -Depth 6 -Compress
"""
        facts = self.run_probe(body)
        self.assertFalse(facts["complete"])
        self.assertIn("path limit", facts["incomplete"])
        self.assertEqual(facts["notified"], [], "an unsupported path must not be announced")

    def test_a_missing_icon_asset_refuses_before_any_save(self) -> None:
        body = r"""
Remove-Item -LiteralPath (Join-Path $installRoot 'desktop\python-webview-shell\assets\WorkStack-Mark-Lime-v2.ico') -Force
$failed = $false
$message = ''
try {
    Invoke-WorkStackTestFinalization -InstallPath $installRoot -StatePath $stateRoot `
        -StartMenuPath $startMenu -DesktopPath $desktop `
        -ShortcutFactory $shortcutFactory -Notifier $notifier -ExistenceProbe $existenceProbe | Out-Null
} catch { $failed = $true; $message = $_.Exception.Message }
[ordered]@{
    saved = @($script:Saved); notified = @($script:Notified)
    complete = $failed; incomplete = $message
} | ConvertTo-Json -Depth 6 -Compress
"""
        facts = self.run_probe(body)
        self.assertTrue(facts["complete"], "a missing icon must refuse")
        self.assertEqual(facts["saved"], [], "no link may be saved without the icon")
        self.assertEqual(facts["notified"], [])

    def test_a_partial_save_failure_notifies_only_what_was_saved(self) -> None:
        body = r"""
$script:FailSaveAt = 1
$failed = $false
try {
    Invoke-WorkStackTestFinalization -InstallPath $installRoot -StatePath $stateRoot `
        -StartMenuPath $startMenu -DesktopPath $desktop `
        -ShortcutFactory $shortcutFactory -Notifier $notifier -ExistenceProbe $existenceProbe | Out-Null
} catch { $failed = $true }
[ordered]@{
    saved = @($script:Saved); notified = @($script:Notified)
    complete = $failed; incomplete = ''
} | ConvertTo-Json -Depth 6 -Compress
"""
        facts = self.run_probe(body)
        self.assertTrue(facts["complete"], "a save failure must propagate to the caller")
        self.assertEqual(len(facts["saved"]), 1, "only the first link was saved")
        self.assertEqual(facts["notified"], [],
                         "notification must not run for a partial finalization")

    def test_the_shared_path_guard_matches_the_frozen_policy(self) -> None:
        body = r"""
$programs = Join-Path $installRoot 'Programs'
$outside = Join-Path $installRoot 'Elsewhere\WorkStack'
$inside = Join-Path $programs 'WorkStack'
$results = [ordered]@{}
foreach ($case in @(
    @{ name = 'outside_without_switch'; path = $outside; switch = $false },
    @{ name = 'outside_with_switch'; path = $outside; switch = $true },
    @{ name = 'inside_without_switch'; path = $inside; switch = $false },
    @{ name = 'inside_case_differs'; path = $inside.ToUpperInvariant(); switch = $false }
)) {
    try {
        Assert-WorkStackShortcutInstallPath -InstallPath $case.path -LocalProgramsPath $programs -OriginalNoShortcut $case.switch
        $results[$case.name] = 'allowed'
    } catch { $results[$case.name] = $_.Exception.Message }
}
[ordered]@{ saved = @(); notified = @(); complete = $true; incomplete = ($results | ConvertTo-Json -Compress) } | ConvertTo-Json -Depth 6 -Compress
"""
        facts = self.run_probe(body)
        guard = json.loads(facts["incomplete"])
        self.assertIn("only writes under LOCALAPPDATA", guard["outside_without_switch"])
        self.assertEqual(guard["outside_with_switch"], "allowed",
                         "explicit NoShortcut keeps its existing exception")
        self.assertEqual(guard["inside_without_switch"], "allowed")
        self.assertEqual(guard["inside_case_differs"], "allowed",
                         "the comparison must stay case-insensitive")

    def test_the_probe_refuses_when_a_substitute_is_missing(self) -> None:
        """Fail closed: without the fakes the suite must not reach real effects."""

        environment = dict(os.environ)
        environment["WORKSTACK_PROBE_SOURCE_ROOT"] = str(_source_root() / "no-such-root")
        with tempfile.TemporaryDirectory() as scratch:
            script_path = Path(scratch) / "refuse.ps1"
            script_path.write_text(_EFFECT_PREAMBLE, encoding="utf-8-sig")
            completed = subprocess.run(
                [
                    _powershell(), "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                ],
                capture_output=True, text=True, timeout=120, cwd=scratch, env=environment,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("REFUSE", completed.stdout + completed.stderr)


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()


# --------------------------------------------------------------------------- #
# Executed Apply control flow: the REAL parsed statements, every effect and
# every exit node substituted.
#
# The probe parses the shipped Apply, refuses unless every required command
# node, function node, exit node, updater invocation and rollback-eligible try
# is present, and only then splices recording stubs over the effect leaves and
# the exit nodes and executes the result. The control flow under test -- the
# ordering of the guard, the mutations, the commit boundary, the finalizer and
# every post-commit sink -- is the real source, not a model of it.
#
# Nothing real happens: no updater, launcher, Stop, COM, native call, link or
# icon cache, and no filesystem write outside the substitutes.
# --------------------------------------------------------------------------- #

_APPLY_FLOW_PROBE = _HELPER_PREFLIGHT + r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$root = $env:WORKSTACK_PROBE_SOURCE_ROOT
$scratch = $env:WORKSTACK_PROBE_SCRATCH
$script:Faults = @{}
if ($env:WORKSTACK_PROBE_FAULTS) {
    (ConvertFrom-Json $env:WORKSTACK_PROBE_FAULTS).PSObject.Properties | ForEach-Object {
        $script:Faults[$_.Name] = $_.Value
    }
}
function Test-ProbeFault([string]$Name) { return [bool]$script:Faults[$Name] }
# Sink faults that belong to the DERIVED, post-commit region only. Faulting the
# same sink before the boundary would fail the transaction for an unrelated
# reason and prove nothing about post-commit isolation.
$global:ProbeCommitted = $false
function Test-ProbePostCommitFault([string]$Name) {
    return ((Test-ProbeFault $Name) -and $global:ProbeCommitted)
}

$applyPath = Join-Path $root 'scripts\windows\Apply-WorkStackUpdate.ps1'
$helperDefinitions = Get-WorkStackTestHelperDefinitions (Join-Path $root 'scripts\windows\WorkStack-Shortcuts.ps1')
if (-not (Test-Path -LiteralPath $applyPath -PathType Leaf)) {
    throw "REFUSE: Apply is missing: $applyPath"
}
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($applyPath, [ref]$null, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "REFUSE: Apply has $($parseErrors.Count) parse errors" }

# ---- required nodes, verified BEFORE anything is executed -----------------
$commandNodes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))
$functionNodes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))
$exitNodes = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ExitStatementAst] }, $true))
foreach ($required in @(
    'Assert-WorkStackShortcutInstallPath', 'Invoke-WorkStackShortcutFinalization',
    'Invoke-PostCommitStep', 'Get-BoundedDiagnostic', 'New-Item', 'Remove-Item', 'Start-Process'
)) {
    if (@($commandNodes | Where-Object { $_.GetCommandName() -eq $required }).Count -lt 1) {
        throw "REFUSE: required command node is missing: $required"
    }
}
foreach ($required in @(
    'Write-UpdateReceipt', 'Write-UpdateLog', 'Write-BytesAtomic',
    'New-InstallRecoverySnapshot', 'Restore-InstallRecoverySnapshot',
    'Get-BoundedDiagnostic', 'Invoke-PostCommitStep'
)) {
    if (@($functionNodes | Where-Object { $_.Name -eq $required }).Count -lt 1) {
        throw "REFUSE: required function node is missing: $required"
    }
}
if ($exitNodes.Count -lt 3) { throw "REFUSE: expected exit nodes, found $($exitNodes.Count)" }
$updaterCall = @($commandNodes | Where-Object { $_.Extent.Text -match '\$updater\b' }) | Select-Object -First 1
if (-not $updaterCall) { throw 'REFUSE: the updater invocation node is missing' }
$rollbackTry = @($ast.FindAll({
    param($n) $n -is [System.Management.Automation.Language.TryStatementAst]
}, $true) | Where-Object {
    ($_.CatchClauses.Extent.Text -join "`n") -match 'Restore-InstallRecoverySnapshot'
}) | Select-Object -First 1
if (-not $rollbackTry) { throw 'REFUSE: the rollback-eligible try node is missing' }

# ---- splice substitutes over the effect leaves and the exit nodes ---------
$text = [IO.File]::ReadAllText($applyPath)
$edits = New-Object System.Collections.ArrayList
$stubs = @{
    'Write-UpdateReceipt' = @'
function Write-UpdateReceipt {
    param([Parameter(Mandatory = $true)][string]$Status, [string]$Message = '', [string]$RecoveryPath = '')
    if (Test-ProbePostCommitFault 'receipt') { throw 'Injected receipt write failure.' }
    [void]$global:Effects.Add([pscustomobject]@{ kind = 'receipt'; status = $Status; message = $Message; length = $Message.Length })
}
'@
    'Write-UpdateLog' = @'
function Write-UpdateLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    if (Test-ProbePostCommitFault 'log') { throw 'Injected update log failure.' }
    [void]$global:Effects.Add([pscustomobject]@{ kind = 'log'; message = $Message; length = $Message.Length })
}
'@
    'Write-BytesAtomic' = @'
function Write-BytesAtomic {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][byte[]]$Bytes)
    [void]$global:Effects.Add([pscustomobject]@{ kind = 'write-bytes'; path = $Path })
}
'@
    'New-InstallRecoverySnapshot' = @'
function New-InstallRecoverySnapshot {
    if (Test-ProbeFault 'snapshot') { throw 'Injected recovery snapshot failure.' }
    [void]$global:Effects.Add([pscustomobject]@{ kind = 'snapshot'; path = '' })
}
'@
    'Restore-InstallRecoverySnapshot' = @'
function Restore-InstallRecoverySnapshot {
    if (Test-ProbeFault 'restore') { throw 'Injected update recovery failure.' }
    [void]$global:Effects.Add([pscustomobject]@{ kind = 'restore'; path = '' })
}
'@
}
foreach ($node in $functionNodes) {
    if ($stubs.ContainsKey($node.Name)) {
        [void]$edits.Add([pscustomobject]@{ Start = $node.Extent.StartOffset; End = $node.Extent.EndOffset; Text = $stubs[$node.Name] })
    }
}
foreach ($node in $exitNodes) {
    $code = ($node.Extent.Text -replace '[^0-9]', '')
    if (-not $code) { $code = '0' }
    [void]$edits.Add([pscustomobject]@{ Start = $node.Extent.StartOffset; End = $node.Extent.EndOffset; Text = ("Invoke-SubstitutedExit {0}" -f $code) })
}
[void]$edits.Add([pscustomobject]@{ Start = $updaterCall.Extent.StartOffset; End = $updaterCall.Extent.EndOffset; Text = 'Invoke-SubstitutedUpdater' })
foreach ($edit in @($edits | Sort-Object Start -Descending)) {
    $text = $text.Substring(0, $edit.Start) + $edit.Text + $text.Substring($edit.End)
}
# The only context substitution: a created ScriptBlock has no backing file,
# so $PSScriptRoot would be empty. Everything else executes as written.
$text = $text.Replace('$PSScriptRoot', '$global:ProbeScriptRoot')

# ---- substitutes. Every external effect is bound here, before execution ----
$global:Effects = New-Object System.Collections.ArrayList
$script:Existing = @{}
function Add-Effect([string]$Kind, [string]$Path) {
    [void]$global:Effects.Add([pscustomobject]@{ kind = $Kind; path = $Path; message = ''; status = ''; length = 0 })
}
function New-Item {
    param($ItemType, $Path, [switch]$Force)
    foreach ($entry in @($Path)) { Add-Effect 'mkdir' ([string]$entry) }
    if (Test-ProbeFault 'mkdir') { throw 'Injected directory creation failure.' }
}
function Remove-Item {
    param($LiteralPath, [switch]$Recurse, [switch]$Force)
    $target = [string]$LiteralPath
    Add-Effect 'remove' $target
    if ((Test-ProbePostCommitFault 'remove_runner') -and $target -like '*.apply-*') { throw 'Injected runner cleanup failure.' }
    if ((Test-ProbePostCommitFault 'remove_recovery') -and $target -like '*.rollback-*') { throw 'Injected recovery cleanup failure.' }
}
function Copy-Item { param($LiteralPath, $Destination, [switch]$Recurse, [switch]$Force) Add-Effect 'copy' ([string]$Destination) }
function Move-Item { param($LiteralPath, $Destination, [switch]$Force) Add-Effect 'move' ([string]$Destination) }
function Test-Path {
    param($LiteralPath, $PathType)
    $target = [string]$LiteralPath
    if ($script:Existing.ContainsKey($target)) { return [bool]$script:Existing[$target] }
    # Recovery and runner directories are treated as present so their cleanup
    # is actually attempted; everything else the script probes exists.
    return $true
}
function Get-Process { param($Id, $ErrorAction) return $null }
function Wait-Process { param($Id, $Timeout) Add-Effect 'wait-process' ([string]$Id) }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru)
    Add-Effect 'start-process' ([string]$FilePath)
    $process = [pscustomobject]@{ Id = 4242; ExitCode = 9 }
    $process | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $false }
    $immediate = Test-ProbeFault 'launcher_exits'
    $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
        param($Milliseconds)
        # Returning false is the acceptance signal, so the commit boundary is
        # reached immediately after this call.
        if (-not $immediate) { $global:ProbeCommitted = $true }
        return $immediate
    }.GetNewClosure()
    return $process
}
function Write-Warning {
    param($Message)
    if (Test-ProbePostCommitFault 'warning') { throw 'Injected warning sink failure.' }
    [void]$global:Effects.Add([pscustomobject]@{ kind = 'warning'; message = [string]$Message; length = ([string]$Message).Length; path = ''; status = '' })
}
function Write-Error {
    param($Message, $ErrorAction)
    [void]$global:Effects.Add([pscustomobject]@{ kind = 'error'; message = [string]$Message; length = 0; path = ''; status = '' })
}
function Invoke-SubstitutedUpdater {
    Add-Effect 'updater' ''
    if (Test-ProbeFault 'updater') { $global:LASTEXITCODE = 3 } else { $global:LASTEXITCODE = 0 }
}
function Invoke-SubstitutedExit { param([int]$Code) throw "PROBE-EXIT-$Code" }

# ---- distinct pre-update and installed helper implementations (R3) --------
$preUpdateScripts = Join-Path $scratch 'preupdate\scripts\windows'
# The default install root is inside the CONTAINED LOCALAPPDATA\Programs, which
# is what the ordinary accepted flow requires; the override arrives before the
# installed helper is written so an outside-path case still stages correctly.
$installRoot = Join-Path $env:LOCALAPPDATA ('Programs\WorkStack-' + [guid]::NewGuid().ToString('N'))
$installOverride = $env:WORKSTACK_PROBE_INSTALL_ROOT
if ($installOverride) { $installRoot = $installOverride }
$installedScripts = Join-Path $installRoot 'scripts\windows'
$realHelper = $helperDefinitions
foreach ($pair in @(
    @{ dir = $preUpdateScripts; tag = 'preupdate' },
    @{ dir = $installedScripts; tag = 'installed' }
)) {
    [void][IO.Directory]::CreateDirectory($pair.dir)
    $marked = $realHelper + @"

function Invoke-WorkStackShortcutFinalization {
    param(`$InstallPath, `$StatePath, `$StartMenuPath, `$DesktopPath, `$ShortcutFactory, `$Notifier, `$ExistenceProbe)
    [void]`$global:Effects.Add([pscustomobject]@{ kind = 'finalize'; path = '$($pair.tag)'; message = ''; status = ''; length = 0 })
    if (Test-ProbeFault 'finalizer') { throw (Test-ProbeDiagnostic) }
    if (Test-ProbeFault 'finalizer_incomplete') {
        return [pscustomobject]@{ Saved = @(); Notifications = @(); Complete = `$false; IncompleteReason = (Test-ProbeDiagnostic) }
    }
    return [pscustomobject]@{ Saved = @(); Notifications = @(); Complete = `$true; IncompleteReason = '' }
}
function New-WorkStackShellChangeNotifier { throw 'REFUSE: real native notifier is forbidden in this fixture' }
"@
    [IO.File]::WriteAllText((Join-Path $pair.dir 'WorkStack-Shortcuts.ps1'), $marked)
}
if (Test-ProbeFault 'missing_installed_helper') {
    [IO.File]::Delete((Join-Path $installedScripts 'WorkStack-Shortcuts.ps1'))
}
function Test-ProbeDiagnostic {
    if (Test-ProbeFault 'long_diagnostic') { return ('D' * 2000) }
    return 'substituted derived failure'
}

# ---- execute the real, substituted flow ----------------------------------
$global:ProbeScriptRoot = $preUpdateScripts
$stateRoot = Join-Path $scratch 'state'
$noShortcut = [bool]$env:WORKSTACK_PROBE_NOSHORTCUT

$observedExit = $null
$escaped = ''
try {
    & ([ScriptBlock]::Create($text)) `
        -SetupPath (Join-Path $scratch 'setup.exe') -ChecksumPath (Join-Path $scratch 'setup.sha256') `
        -InstallRoot $installRoot -StateRoot $stateRoot -ParentProcessId 4321 `
        -TargetVersion '1.2.3' -NoShortcut:$noShortcut
} catch {
    if ($_.Exception.Message -match '^PROBE-EXIT-(\d+)$') { $observedExit = [int]$Matches[1] }
    else { $escaped = $_.Exception.Message }
}

[ordered]@{
    exit_code = $observedExit
    escaped = $escaped
    effects = @($global:Effects)
    kinds = @($global:Effects | ForEach-Object { $_.kind })
} | ConvertTo-Json -Depth 6 -Compress
"""


class SubstitutedApplyFlow(unittest.TestCase):
    """Execute the shipped Apply control flow with every effect substituted."""

    def run_flow(self, *, faults=(), install_root: str | None = None,
                 no_shortcut: bool = False, environment_faults=None) -> dict:
        environment = dict(os.environ)
        environment["WORKSTACK_PROBE_SOURCE_ROOT"] = str(_source_root())
        environment["WORKSTACK_PROBE_FAULTS"] = json.dumps({name: True for name in faults})
        environment["WORKSTACK_PROBE_NOSHORTCUT"] = "1" if no_shortcut else ""
        for name in (
            "WORKSTACK_UPDATE_TEST_FAIL_LAUNCHER_VALIDATION",
            "WORKSTACK_UPDATE_TEST_FAIL_RESTART",
            "WORKSTACK_UPDATE_TEST_FAIL_ROLLBACK",
        ):
            environment.pop(name, None)
        for name in environment_faults or ():
            environment[name] = "1"
        with tempfile.TemporaryDirectory() as scratch:
            environment["WORKSTACK_PROBE_SCRATCH"] = scratch
            if install_root is not None:
                environment["WORKSTACK_PROBE_INSTALL_ROOT"] = install_root
            else:
                environment.pop("WORKSTACK_PROBE_INSTALL_ROOT", None)
            script_path = Path(scratch) / "flow.ps1"
            script_path.write_text(_APPLY_FLOW_PROBE, encoding="utf-8-sig")
            completed = subprocess.run(
                [
                    _powershell(), "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                ],
                capture_output=True, text=True, timeout=240,
                cwd=scratch, env=environment,
            )
            return _parse_json_result(completed)

    @staticmethod
    def _kinds(facts: dict) -> list:
        return [_text(kind) for kind in facts["kinds"]]

    @staticmethod
    def _of(facts: dict, kind: str) -> list:
        return [entry for entry in facts["effects"] if _text(entry["kind"]) == kind]

    # -- the fixture itself must fail closed ------------------------------
    def test_the_flow_probe_refuses_when_the_source_is_absent(self) -> None:
        """A missing or malformed source must refuse, never fall through to
        real effects."""

        environment = dict(os.environ)
        environment["WORKSTACK_PROBE_SOURCE_ROOT"] = str(Path(tempfile.gettempdir()) / "workstack-absent-source")
        with tempfile.TemporaryDirectory() as scratch:
            environment["WORKSTACK_PROBE_SCRATCH"] = scratch
            environment["WORKSTACK_PROBE_FAULTS"] = "{}"
            script_path = Path(scratch) / "flow.ps1"
            script_path.write_text(_APPLY_FLOW_PROBE, encoding="utf-8-sig")
            completed = subprocess.run(
                [
                    _powershell(), "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                ],
                capture_output=True, text=True, timeout=240, cwd=scratch, env=environment,
            )
        self.assertNotEqual(completed.returncode, 0, "an absent source must refuse")
        self.assertIn("REFUSE", completed.stdout + completed.stderr)

    def test_the_flow_probe_refuses_a_source_missing_a_required_node(self) -> None:
        """Deleting a required node from a copy must refuse before execution,
        so malformed source cannot pass by doing nothing."""

        with tempfile.TemporaryDirectory() as mutilated:
            windows = Path(mutilated) / "scripts" / "windows"
            windows.mkdir(parents=True)
            for name in (APPLY, HELPER):
                (windows / name).write_bytes((_windows_script(name)).read_bytes())
            apply_text = (windows / APPLY).read_text(encoding="utf-8")
            apply_text = apply_text.replace("function Invoke-PostCommitStep {", "function Invoke-RenamedStep {")
            (windows / APPLY).write_text(apply_text, encoding="utf-8")
            environment = dict(os.environ)
            environment["WORKSTACK_PROBE_SOURCE_ROOT"] = mutilated
            with tempfile.TemporaryDirectory() as scratch:
                environment["WORKSTACK_PROBE_SCRATCH"] = scratch
                environment["WORKSTACK_PROBE_FAULTS"] = "{}"
                script_path = Path(scratch) / "flow.ps1"
                script_path.write_text(_APPLY_FLOW_PROBE, encoding="utf-8-sig")
                completed = subprocess.run(
                    [
                        _powershell(), "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                    ],
                    capture_output=True, text=True, timeout=240, cwd=scratch, env=environment,
                )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("required function node is missing", completed.stdout + completed.stderr)

    # -- success control ---------------------------------------------------
    def test_the_committed_success_path_reaches_every_sink_and_exit_zero(self) -> None:
        facts = self.run_flow()
        kinds = self._kinds(facts)
        self.assertEqual(facts["exit_code"], 0, facts)
        self.assertEqual(_text(facts["escaped"]), "")
        self.assertIn("updater", kinds)
        self.assertIn("start-process", kinds)
        self.assertIn("finalize", kinds)
        receipts = self._of(facts, "receipt")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(_text(receipts[0]["status"]), "installed")
        self.assertEqual(_text(receipts[0]["message"]), "")
        self.assertTrue(self._of(facts, "log"))
        self.assertFalse(self._of(facts, "warning"))
        self.assertFalse(self._of(facts, "restore"))

    # -- R3: the INSTALLED helper finalizes, not the pre-update one --------
    def test_only_the_installed_helper_finalizes_after_acceptance(self) -> None:
        facts = self.run_flow()
        finalizers = self._of(facts, "finalize")
        self.assertEqual(len(finalizers), 1, "exactly one finalization must run")
        self.assertEqual(
            _text(finalizers[0]["path"]), "installed",
            "the pre-update definitions must not be what finalizes after acceptance",
        )

    def test_a_missing_installed_helper_is_an_applied_with_warning(self) -> None:
        facts = self.run_flow(faults=("missing_installed_helper",))
        self.assertEqual(facts["exit_code"], 0)
        receipts = self._of(facts, "receipt")
        self.assertEqual(_text(receipts[0]["status"]), "installed")
        self.assertTrue(_text(receipts[0]["message"]).startswith("Shortcuts incomplete: "))
        self.assertFalse(self._of(facts, "restore"))

    # -- R1: committed runner cleanup cannot suppress reporting ------------
    def test_a_committed_runner_cleanup_failure_still_reports_and_exits_zero(self) -> None:
        facts = self.run_flow(faults=("remove_runner",))
        self.assertEqual(facts["exit_code"], 0, facts)
        self.assertEqual(_text(facts["escaped"]), "", "the cleanup failure must not escape")
        receipts = self._of(facts, "receipt")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(_text(receipts[0]["status"]), "installed")
        self.assertTrue(self._of(facts, "log"))
        self.assertTrue(self._of(facts, "finalize"))
        self.assertFalse(self._of(facts, "restore"))

    def test_a_precommit_runner_cleanup_still_runs_for_an_uncommitted_run(self) -> None:
        facts = self.run_flow(faults=("updater",))
        removed = [_text(entry["path"]) for entry in self._of(facts, "remove")]
        self.assertTrue(
            any(".apply-" in path for path in removed),
            "an uncommitted run must still remove its runner: " + repr(removed),
        )
        self.assertEqual(facts["exit_code"], 1)

    # -- R2: every post-commit sink is independently protected -------------
    def test_a_receipt_failure_does_not_prevent_the_log_or_exit_zero(self) -> None:
        facts = self.run_flow(faults=("receipt",))
        self.assertEqual(facts["exit_code"], 0)
        logs = [_text(entry["message"]) for entry in self._of(facts, "log")]
        self.assertTrue(any("receipt could not be written" in line for line in logs), logs)
        self.assertTrue(self._of(facts, "warning"))

    def test_a_failure_while_warning_about_a_failure_still_reaches_exit_zero(self) -> None:
        """The reporting sink that reports another sink's failure is itself a
        sink, so its failure must not abort the committed completion."""

        facts = self.run_flow(faults=("receipt", "warning"))
        self.assertEqual(facts["exit_code"], 0, facts)
        self.assertEqual(_text(facts["escaped"]), "")
        self.assertFalse(self._of(facts, "warning"), "the warning sink was faulted")
        self.assertTrue(self._of(facts, "log"), "later sinks must still be attempted")

    def test_a_log_failure_does_not_prevent_the_recovery_cleanup_or_exit_zero(self) -> None:
        facts = self.run_flow(faults=("log",))
        self.assertEqual(facts["exit_code"], 0)
        removed = [_text(entry["path"]) for entry in self._of(facts, "remove")]
        self.assertTrue(any(".rollback-" in path for path in removed), removed)
        self.assertTrue(any(".apply-" in path for path in removed), removed)

    def test_every_post_commit_sink_can_fail_at_once_and_still_exit_zero(self) -> None:
        facts = self.run_flow(
            faults=("finalizer", "receipt", "log", "warning", "remove_recovery", "remove_runner")
        )
        self.assertEqual(facts["exit_code"], 0, facts)
        self.assertEqual(_text(facts["escaped"]), "")
        self.assertFalse(self._of(facts, "restore"))

    def test_derived_diagnostics_are_bounded_before_they_reach_any_sink(self) -> None:
        facts = self.run_flow(faults=("finalizer", "long_diagnostic"))
        self.assertEqual(facts["exit_code"], 0)
        receipts = self._of(facts, "receipt")
        message = _text(receipts[0]["message"])
        self.assertTrue(message.startswith("Shortcuts incomplete: "))
        # 22 characters of prefix plus the documented 300-character payload.
        self.assertLessEqual(len(message), 322)
        self.assertTrue(message.endswith("..."), message[-16:])
        for entry in self._of(facts, "warning") + self._of(facts, "log"):
            self.assertLess(
                len(_text(entry["message"])), 500,
                "no sink may receive a raw unbounded diagnostic",
            )

    def test_an_incomplete_finalization_is_bounded_the_same_way(self) -> None:
        facts = self.run_flow(faults=("finalizer_incomplete", "long_diagnostic"))
        self.assertEqual(facts["exit_code"], 0)
        message = _text(self._of(facts, "receipt")[0]["message"])
        self.assertLessEqual(len(message), 322)

    # -- R7: the original-intent guard precedes the FIRST mutation ---------
    def test_an_outside_install_path_refuses_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            facts = self.run_flow(install_root=str(Path(outside) / "Elsewhere" / "WorkStack"))
        self.assertEqual(facts["exit_code"], 1, facts)
        kinds = self._kinds(facts)
        for forbidden in ("mkdir", "updater", "snapshot", "finalize", "copy", "move", "start-process"):
            self.assertNotIn(forbidden, kinds, "refusal must precede " + forbidden)
        errors = [_text(entry["message"]) for entry in self._of(facts, "error")]
        self.assertTrue(
            any("only writes under" in message for message in errors), errors
        )

    def test_the_explicit_noshortcut_exception_still_installs_outside(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            facts = self.run_flow(
                install_root=str(Path(outside) / "Elsewhere" / "WorkStack"), no_shortcut=True
            )
        self.assertEqual(facts["exit_code"], 0, facts)
        kinds = self._kinds(facts)
        self.assertIn("mkdir", kinds)
        self.assertIn("updater", kinds)
        self.assertNotIn("finalize", kinds, "an explicit -NoShortcut must finalize nothing")

    def test_an_inside_path_is_accepted_case_insensitively(self) -> None:
        programs = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "WorkStack"
        facts = self.run_flow(install_root=str(programs).upper())
        self.assertEqual(facts["exit_code"], 0, facts)
        self.assertIn("mkdir", self._kinds(facts))

    # -- pre-commit vectors keep their rollback semantics -------------------
    def test_an_updater_failure_rolls_back_and_never_finalizes(self) -> None:
        facts = self.run_flow(faults=("updater",))
        self.assertEqual(facts["exit_code"], 1)
        kinds = self._kinds(facts)
        self.assertNotIn("finalize", kinds)
        self.assertNotIn("restore", kinds, "the updater failed before installation applied")
        self.assertEqual(_text(self._of(facts, "receipt")[0]["status"]), "failed")

    def test_a_launcher_validation_failure_restores_and_never_finalizes(self) -> None:
        facts = self.run_flow(
            environment_faults=("WORKSTACK_UPDATE_TEST_FAIL_LAUNCHER_VALIDATION",)
        )
        self.assertEqual(facts["exit_code"], 1)
        self.assertTrue(self._of(facts, "restore"))
        self.assertNotIn("finalize", self._kinds(facts))
        self.assertEqual(_text(self._of(facts, "receipt")[0]["status"]), "rolled-back")

    def test_a_restart_failure_restores_and_never_finalizes(self) -> None:
        facts = self.run_flow(environment_faults=("WORKSTACK_UPDATE_TEST_FAIL_RESTART",))
        self.assertEqual(facts["exit_code"], 1)
        self.assertTrue(self._of(facts, "restore"))
        self.assertNotIn("finalize", self._kinds(facts))

    def test_an_immediate_launcher_exit_restores_and_never_finalizes(self) -> None:
        facts = self.run_flow(faults=("launcher_exits",))
        self.assertEqual(facts["exit_code"], 1)
        self.assertTrue(self._of(facts, "restore"))
        self.assertNotIn("finalize", self._kinds(facts))

    def test_a_restore_failure_reports_recovery_required_and_never_finalizes(self) -> None:
        facts = self.run_flow(
            faults=("restore",),
            environment_faults=("WORKSTACK_UPDATE_TEST_FAIL_RESTART",),
        )
        self.assertEqual(facts["exit_code"], 1)
        self.assertEqual(_text(self._of(facts, "receipt")[0]["status"]), "recovery-required")
        self.assertNotIn("finalize", self._kinds(facts))

    def test_a_snapshot_failure_never_reaches_the_updater(self) -> None:
        facts = self.run_flow(faults=("snapshot",))
        self.assertEqual(facts["exit_code"], 1)
        kinds = self._kinds(facts)
        self.assertNotIn("updater", kinds)
        self.assertNotIn("finalize", kinds)

    def test_the_transactional_install_is_always_told_to_suppress_links(self) -> None:
        """Forcing suppression internally must not change what the guard read."""

        text = _windows_script(APPLY).read_text(encoding="utf-8")
        self.assertIn("-NoShortcut:$true", text)
        self.assertIn("$originalNoShortcut = [bool]$NoShortcut", text)
        self.assertIn("-OriginalNoShortcut $originalNoShortcut", text)


# --------------------------------------------------------------------------- #
# R5 / R6: the notification input contract and the staged asset guard.
# --------------------------------------------------------------------------- #


class NotificationLengthBoundary(unittest.TestCase):
    """MAX_PATH counts the terminating NUL, so 259 fits and 260 does not.

    The substituted probe runner is reused directly rather than inherited, so
    this class contributes its own case instead of re-running the effect suite.
    """

    run_probe = SubstitutedFinalizationEffects.run_probe

    LENGTH_BODY = r"""
$results = @()
foreach ($length in @(259, 260, 261)) {
    $script:Notified.Clear()
    $path = 'C:\' + ('n' * ($length - 3))
    $outcome = Invoke-WorkStackTestNotification -Path $path -Existed $false -Notifier $notifier
    $results += [pscustomobject]@{
        length = $path.Length
        notified = $outcome.Notified
        reason = $outcome.Reason
        announcements = $script:Notified.Count
    }
}
[ordered]@{ results = @($results) } | ConvertTo-Json -Depth 6 -Compress
"""

    def test_259_is_announced_and_260_and_261_are_refused(self) -> None:
        facts = self.run_probe(self.LENGTH_BODY)
        by_length = {entry["length"]: entry for entry in facts["results"]}
        self.assertEqual(sorted(by_length), [259, 260, 261])
        self.assertTrue(by_length[259]["notified"], "259 non-NUL characters fit MAX_PATH")
        self.assertEqual(by_length[259]["announcements"], 1)
        for refused in (260, 261):
            self.assertFalse(by_length[refused]["notified"], refused)
            self.assertEqual(
                by_length[refused]["announcements"], 0,
                "a refused path must produce zero announcements",
            )
            self.assertIn("limit", _text(by_length[refused]["reason"]))


_STAGED_ICON_PROBE = _HELPER_PREFLIGHT + r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$root = $env:WORKSTACK_PROBE_SOURCE_ROOT
$helper = Join-Path $root 'scripts\windows\WorkStack-Shortcuts.ps1'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw "REFUSE: helper missing: $helper" }
$helperDefinitions = Get-WorkStackTestHelperDefinitions $helper
. ([scriptblock]::Create($helperDefinitions))
function New-WorkStackShellChangeNotifier { throw 'REFUSE: real native notifier is forbidden in this fixture' }
foreach ($required in @('Assert-WorkStackShortcutIconAsset', 'Get-WorkStackShortcutIconPath')) {
    if (-not (Get-Command -Name $required -CommandType Function -ErrorAction SilentlyContinue)) {
        throw "REFUSE: required function is missing: $required"
    }
}
# Destructive events are substituted; none of them may be reached.
$script:Destructive = New-Object System.Collections.ArrayList
function Stop-WorkStackRuntime { [void]$script:Destructive.Add('stop') }
function Backup-WorkStackData { [void]$script:Destructive.Add('backup') }
function Move-PayloadIntoPlace { [void]$script:Destructive.Add('move') }

$results = @()
foreach ($case in @('present', 'missing', 'directory')) {
    $staging = Join-Path $env:WORKSTACK_PROBE_SCRATCH ("staging-" + $case)
    $assets = Join-Path $staging 'desktop\python-webview-shell\assets'
    [void][IO.Directory]::CreateDirectory($assets)
    $leaf = Join-Path $assets 'WorkStack-Mark-Lime-v2.ico'
    if ($case -eq 'present') { [IO.File]::WriteAllBytes($leaf, [byte[]](1, 2, 3)) }
    if ($case -eq 'directory') { [void][IO.Directory]::CreateDirectory($leaf) }
    $script:Destructive.Clear()
    $refused = $false
    $message = ''
    try {
        Assert-WorkStackShortcutIconAsset -IconPath (Get-WorkStackShortcutIconPath -InstallPath $staging)
        Stop-WorkStackRuntime; Backup-WorkStackData; Move-PayloadIntoPlace
    } catch { $refused = $true; $message = $_.Exception.Message }
    $results += [pscustomobject]@{
        case = $case; refused = $refused; message = $message
        destructive = $script:Destructive.Count
    }
}
[ordered]@{ results = @($results) } | ConvertTo-Json -Depth 6 -Compress
"""


class StagedIconGuard(unittest.TestCase):
    """The staged leaf is validated after the copy and before destructive work."""

    @classmethod
    def setUpClass(cls) -> None:
        environment = dict(os.environ)
        environment["WORKSTACK_PROBE_SOURCE_ROOT"] = str(_source_root())
        with tempfile.TemporaryDirectory() as scratch:
            environment["WORKSTACK_PROBE_SCRATCH"] = scratch
            script_path = Path(scratch) / "staged.ps1"
            script_path.write_text(_STAGED_ICON_PROBE, encoding="utf-8-sig")
            completed = subprocess.run(
                [
                    _powershell(), "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                ],
                capture_output=True, text=True, timeout=180, cwd=scratch, env=environment,
            )
            cls.facts = _parse_json_result(completed)

    def _case(self, name: str) -> dict:
        for entry in self.facts["results"]:
            if _text(entry["case"]) == name:
                return entry
        raise AssertionError("missing case " + name)

    def test_a_present_staged_leaf_permits_the_destructive_events(self) -> None:
        entry = self._case("present")
        self.assertFalse(entry["refused"])
        self.assertEqual(entry["destructive"], 3)

    def test_a_missing_staged_leaf_refuses_before_any_destructive_event(self) -> None:
        entry = self._case("missing")
        self.assertTrue(entry["refused"])
        self.assertEqual(entry["destructive"], 0)
        self.assertIn("icon is missing", _text(entry["message"]))

    def test_a_staged_directory_is_not_accepted_as_the_icon_leaf(self) -> None:
        entry = self._case("directory")
        self.assertTrue(entry["refused"])
        self.assertEqual(entry["destructive"], 0)


class StagedIconGuardOrdering(unittest.TestCase):
    """Where the staged guard sits in Install's real parsed statement order."""

    def setUp(self) -> None:
        self.text = _windows_script(INSTALL).read_text(encoding="utf-8")

    def _offset(self, needle: str) -> int:
        index = self.text.find(needle)
        self.assertNotEqual(index, -1, "Install no longer contains " + needle)
        return index

    def test_the_staged_guard_follows_the_copy_and_precedes_destructive_work(self) -> None:
        staged = self._offset(
            "Assert-WorkStackShortcutIconAsset -IconPath (Get-WorkStackShortcutIconPath -InstallPath $staging)"
        )
        source = self._offset(
            "Assert-WorkStackShortcutIconAsset -IconPath (Get-WorkStackShortcutIconPath -InstallPath $sourcePath)"
        )
        copy = self._offset("Copy-Item -LiteralPath (Join-Path $sourcePath 'scripts\\windows')")
        stop = self._offset("Stop-WorkStack.ps1")
        move = self._offset("Move-Item -LiteralPath $staging -Destination $installPath")
        self.assertLess(source, copy, "the source check still precedes staging")
        self.assertLess(copy, staged, "the staged check must follow the copy")
        self.assertLess(staged, stop, "the staged check must precede Stop")
        self.assertLess(staged, move, "the staged check must precede the payload move")


class HelperPreflightContract(unittest.TestCase):
    """Every executable fixture validates the captured helper before loading it."""

    def run_copied_helper(self, probe: str, mutation: str | None) -> dict:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            windows = root / "source/scripts/windows"
            windows.mkdir(parents=True)
            for name in (APPLY, HELPER):
                text = _windows_script(name).read_text(encoding="utf-8-sig")
                if name == HELPER and mutation == "statement":
                    # An in-memory marker only; never an actual external effect.
                    text = "$global:ProbeSentinel += 1\n" + text
                elif name == HELPER and mutation == "function":
                    text = text.replace("function Get-WorkStackManagedShortcut {",
                                        "function Get-UnexpectedShortcut {")
                (windows / name).write_text(text, encoding="utf-8-sig")
            probe_file = root / "probe.ps1"
            probe_file.write_text(probe, encoding="utf-8-sig")
            wrapper = root / "wrapper.ps1"
            wrapper.write_text(
                "$global:ProbeSentinel=0\n$failure=''\n"
                + "try { & '" + str(probe_file).replace("'", "''")
                + "' | Out-Null } catch {$failure=$_.Exception.Message}\n"
                + "@{sentinel=$global:ProbeSentinel;failure=$failure} | ConvertTo-Json -Compress\n",
                encoding="utf-8-sig",
            )
            environment = dict(os.environ)
            environment.update(
                WORKSTACK_PROBE_SOURCE_ROOT=str(windows.parents[1]),
                WORKSTACK_PROBE_SCRATCH=str(root / "effects"),
                WORKSTACK_PROBE_FAULTS="{}", WORKSTACK_PROBE_NOSHORTCUT="",
            )
            environment.pop("WORKSTACK_PROBE_INSTALL_ROOT", None)
            (root / "effects").mkdir()
            completed = subprocess.run(
                [_powershell(), "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                cwd=scratch, env=environment, capture_output=True, text=True, timeout=120,
            )
            return _parse_json_result(completed)

    def test_each_harness_rejects_a_top_level_statement_before_it_runs(self) -> None:
        for name, probe in (("helper", _EFFECT_PREAMBLE), ("staged", _STAGED_ICON_PROBE),
                            ("apply", _APPLY_FLOW_PROBE)):
            with self.subTest(harness=name):
                facts = self.run_copied_helper(probe, "statement")
                self.assertEqual(facts["sentinel"], 0)
                self.assertTrue(_text(facts["failure"]).startswith("REFUSE:"), facts)

    def test_each_harness_requires_the_expected_helper_functions(self) -> None:
        for name, probe in (("helper", _EFFECT_PREAMBLE), ("staged", _STAGED_ICON_PROBE),
                            ("apply", _APPLY_FLOW_PROBE)):
            with self.subTest(harness=name):
                facts = self.run_copied_helper(probe, "function")
                self.assertEqual(facts["sentinel"], 0)
                self.assertIn("REFUSE: required helper function", _text(facts["failure"]))

    def test_each_harness_accepts_the_unchanged_functions_only_helper(self) -> None:
        for name, probe in (("helper", _EFFECT_PREAMBLE), ("staged", _STAGED_ICON_PROBE),
                            ("apply", _APPLY_FLOW_PROBE)):
            with self.subTest(harness=name):
                facts = self.run_copied_helper(probe, None)
                self.assertEqual(facts["sentinel"], 0)
                self.assertEqual(_text(facts["failure"]), "")


class RequiredSubstitutionContract(unittest.TestCase):
    run_probe = SubstitutedFinalizationEffects.run_probe

    def test_each_missing_finalizer_substitute_refuses_before_effects(self) -> None:
        facts = self.run_probe(r"""
$outcomes = @()
foreach ($missing in @('StartMenuPath', 'DesktopPath', 'ShortcutFactory', 'Notifier', 'ExistenceProbe')) {
    $arguments = @{InstallPath=$installRoot; StatePath=$stateRoot; StartMenuPath=$startMenu;
        DesktopPath=$desktop; ShortcutFactory=$shortcutFactory; Notifier=$notifier; ExistenceProbe=$existenceProbe}
    [void]$arguments.Remove($missing)
    $failure=''
    try { Invoke-WorkStackTestFinalization @arguments | Out-Null } catch {$failure=$_.Exception.Message}
    $outcomes += @{missing=$missing;failure=$failure;saves=$script:Saved.Count;notifications=$script:Notified.Count}
}
@{outcomes=$outcomes} | ConvertTo-Json -Depth 5 -Compress
""")
        self.assertEqual(len(facts["outcomes"]), 5)
        for outcome in facts["outcomes"]:
            self.assertTrue(_text(outcome["failure"]).startswith("REFUSE: missing"), outcome)
            self.assertIn(outcome["missing"], _text(outcome["failure"]))
            self.assertEqual(outcome["saves"], 0)
            self.assertEqual(outcome["notifications"], 0)

    def test_a_missing_notification_callback_refuses_before_native_fallback(self) -> None:
        facts = self.run_probe(r"""
$failure=''
try { Invoke-WorkStackTestNotification -Path 'C:\fixture.lnk' -Existed $false | Out-Null }
catch {$failure=$_.Exception.Message}
@{failure=$failure;notifications=$script:Notified.Count} | ConvertTo-Json -Compress
""")
        self.assertEqual(_text(facts["failure"]), "REFUSE: missing effect substitute: Notifier")
        self.assertEqual(facts["notifications"], 0)

    def test_real_fallback_leaves_are_rejected_without_calling_them(self) -> None:
        facts = self.run_probe(r"""
$failures=@()
foreach ($attempt in @(
    {New-Object -ComObject WScript.Shell},
    {Add-Type -TypeDefinition 'this must never be compiled'},
    {Send-WorkStackShortcutNotification -Path 'C:\fixture.lnk' -Existed $false},
    {Start-Process 'this must never start'},
    {Stop-Process}
)) {
    try {& $attempt | Out-Null; $failures += 'NOT REFUSED'}
    catch {$failures += $_.Exception.Message}
}
@{failures=$failures;saves=$script:Saved.Count;notifications=$script:Notified.Count} | ConvertTo-Json -Compress
""")
        self.assertEqual(len(facts["failures"]), 5)
        for failure in facts["failures"]:
            self.assertTrue(_text(failure).startswith("REFUSE: real"), failure)
        self.assertEqual(facts["saves"], 0)
        self.assertEqual(facts["notifications"], 0)

    def test_changed_working_directories_fail_the_exact_descriptor_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            windows = Path(scratch) / "scripts/windows"
            windows.mkdir(parents=True)
            text = _windows_script(HELPER).read_text(encoding="utf-8-sig")
            self.assertEqual(text.count("WorkingDirectory = $InstallPath"), 3)
            (windows / HELPER).write_text(
                text.replace("WorkingDirectory = $InstallPath", "WorkingDirectory = 'WRONG-WORKING-DIRECTORY'"),
                encoding="utf-8-sig",
            )
            with mock.patch.dict(os.environ, {"WORKSTACK_SHORTCUT_SOURCE_ROOT": scratch}):
                facts = self.run_probe(SubstitutedFinalizationEffects.SUCCESS_BODY)
            self.assertEqual([entry["WorkingDirectory"] for entry in facts["saved"]],
                             ["WRONG-WORKING-DIRECTORY"] * 3)
            with self.assertRaises(AssertionError):
                SubstitutedFinalizationEffects.assert_exact_descriptors(self, facts)
