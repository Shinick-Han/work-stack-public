[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [Parameter(Mandatory = $true)][string]$ChecksumPath,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][ValidateRange(1, 2147483647)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][ValidatePattern('^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')][string]$TargetVersion,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'
$setup = [IO.Path]::GetFullPath($SetupPath)
$checksum = [IO.Path]::GetFullPath($ChecksumPath)
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$statePath = [IO.Path]::GetFullPath($StateRoot)
$updatesPath = Join-Path $statePath 'updates'
$runnerPath = Join-Path $updatesPath ".apply-$TargetVersion-$PID"
$recoveryPath = Join-Path $updatesPath ".rollback-$TargetVersion-$PID"
$receiptPath = Join-Path $updatesPath 'last-update.json'
$logPath = Join-Path $statePath 'logs\desktop-update.log'
$configPath = Join-Path $statePath 'config.json'
$originalConfigBytes = $null
$originalConfigAttributes = $null
$installationApplied = $false
$restartProcess = $null
# Set at the commit boundary. Reinforces that boundary: post-commit reporting
# runs only when the runtime was accepted, and never re-enters rollback.
$updateCommitted = $false

# Definitions only. Dot-sourcing this file has no effect of any kind, so the
# load is read-only and may precede the install-path guard below.
. (Join-Path $PSScriptRoot 'WorkStack-Shortcuts.ps1')

# The caller's ORIGINAL intent. The transactional install below is always told
# to suppress links, so this is the only surviving record of what the caller
# asked for, and it is what the install-path policy and the finalizer both read.
$originalNoShortcut = [bool]$NoShortcut

# The same policy Install enforces, applied to the ORIGINAL switch and before
# the FIRST mutation of any kind. Only read-only path resolution and the
# definition load above precede it, so a refusal creates no directory, runs no
# updater, takes no snapshot and finalizes nothing. Nothing exists yet to hold
# a receipt or a log line, so the refusal is reported on the error stream and
# exits 1, the same classification the earlier in-transaction refusal produced.
try {
    Assert-WorkStackShortcutInstallPath -InstallPath $installPath -LocalProgramsPath ([IO.Path]::GetFullPath("$env:LOCALAPPDATA\Programs")) -OriginalNoShortcut $originalNoShortcut
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}

New-Item -ItemType Directory -Force -Path $updatesPath, (Split-Path -Parent $logPath) | Out-Null

function Write-UpdateReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Message = '',
        [string]$RecoveryPath = ''
    )
    $receipt = [ordered]@{
        schema_version = 1
        version = $TargetVersion
        status = $Status
        recorded_at = [DateTimeOffset]::UtcNow.ToString('o')
        message = $Message.Substring(0, [Math]::Min($Message.Length, 500))
        recovery_path = if ($RecoveryPath) { [IO.Path]::GetFullPath($RecoveryPath) } else { $null }
    } | ConvertTo-Json -Compress
    $temporary = "$receiptPath.tmp-$PID"
    [IO.File]::WriteAllText($temporary, $receipt, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $receiptPath -Force
}

function Write-UpdateLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $line = "{0} {1}`n" -f [DateTimeOffset]::UtcNow.ToString('o'), $Message
    [IO.File]::AppendAllText($logPath, $line, [Text.UTF8Encoding]::new($false))
}

# Derived diagnostics are bounded BEFORE they reach any sink, so no raw
# exception message can propagate into a warning, the log or the receipt.
# The payload rather than the finished line is bounded because every sink adds
# a constant, predictable prefix: the receipt adds 22 characters of
# 'Shortcuts incomplete: ' (322 total, so its unchanged 500-character cap still
# applies and is simply never reached), a log line adds a 33-character ISO-8601
# timestamp plus one space, and a warning adds its fixed sentence.
$script:DerivedDiagnosticLimit = 300

function Get-BoundedDiagnostic {
    param([AllowNull()][AllowEmptyString()][string]$Text)
    if (-not $Text) { return '' }
    $collapsed = ($Text -replace '\s+', ' ').Trim()
    if ($collapsed.Length -le $script:DerivedDiagnosticLimit) { return $collapsed }
    return $collapsed.Substring(0, $script:DerivedDiagnosticLimit - 3) + '...'
}

function Invoke-PostCommitStep {
    # One derived, post-commit sink, attempted independently. A failure here
    # can never skip the sinks that follow it or the committed exit 0, and it
    # never rolls anything back. Reporting a failure is itself a sink, so the
    # report is wrapped rather than assumed to succeed: this is a controlled
    # fallback for post-commit reporting only, not a blanket swallow, and no
    # pre-commit policy is relaxed by it.
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    try {
        & $Action | Out-Null
        return $true
    } catch {
        $detail = Get-BoundedDiagnostic -Text $_.Exception.Message
        try {
            Write-Warning "Work Stack $TargetVersion is installed, but $($Description): $detail"
        } catch {
            # The last sink available has failed too. There is nowhere left to
            # report, and the update really is installed, so the remaining
            # steps and exit 0 must still happen.
        }
        return $false
    }
}

function Write-BytesAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes
    )
    $temporary = "$Path.tmp-$PID-$([guid]::NewGuid().ToString('N'))"
    try {
        [IO.File]::WriteAllBytes($temporary, $Bytes)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function New-InstallRecoverySnapshot {
    if (-not (Test-Path -LiteralPath $installPath -PathType Container)) {
        throw 'The current Work Stack installation is missing; update recovery cannot be prepared.'
    }
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw 'The current Work Stack configuration is missing; update recovery cannot be prepared.'
    }
    if (Test-Path -LiteralPath $recoveryPath) {
        throw "Update recovery path already exists: $recoveryPath"
    }
    $script:originalConfigBytes = [IO.File]::ReadAllBytes($configPath)
    $script:originalConfigAttributes = [IO.File]::GetAttributes($configPath)
    Copy-Item -LiteralPath $installPath -Destination $recoveryPath -Recurse -Force
    if (-not (Test-Path -LiteralPath $recoveryPath -PathType Container)) {
        throw 'The pre-update installation recovery snapshot was not created.'
    }
}

function Restore-InstallRecoverySnapshot {
    if ([Environment]::GetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_ROLLBACK', 'Process') -ceq '1') {
        throw 'Injected update recovery failure.'
    }
    if (-not (Test-Path -LiteralPath $recoveryPath -PathType Container)) {
        throw 'The pre-update installation recovery snapshot is missing.'
    }
    if ($null -ne $restartProcess -and -not $restartProcess.HasExited) {
        Stop-Process -Id $restartProcess.Id -Force -ErrorAction SilentlyContinue
    }

    $failedInstallPath = "$installPath.failed-$PID"
    if (Test-Path -LiteralPath $failedInstallPath) {
        throw "Update recovery staging path already exists: $failedInstallPath"
    }
    if (Test-Path -LiteralPath $installPath) {
        Move-Item -LiteralPath $installPath -Destination $failedInstallPath
    }
    try {
        Copy-Item -LiteralPath $recoveryPath -Destination $installPath -Recurse -Force
        if (Test-Path -LiteralPath $configPath) {
            [IO.File]::SetAttributes($configPath, [IO.FileAttributes]::Normal)
        }
        Write-BytesAtomic -Path $configPath -Bytes $originalConfigBytes
        [IO.File]::SetAttributes($configPath, $originalConfigAttributes)
        if (Test-Path -LiteralPath $failedInstallPath) {
            Remove-Item -LiteralPath $failedInstallPath -Recurse -Force
        }
        Remove-Item -LiteralPath $recoveryPath -Recurse -Force
    } catch {
        if (Test-Path -LiteralPath $installPath) {
            Remove-Item -LiteralPath $installPath -Recurse -Force
        }
        if (Test-Path -LiteralPath $failedInstallPath) {
            Move-Item -LiteralPath $failedInstallPath -Destination $installPath
        }
        throw
    }
}

try {
    if (-not (Test-Path -LiteralPath $setup -PathType Leaf) -or
        -not (Test-Path -LiteralPath $checksum -PathType Leaf)) {
        throw 'The staged Work Stack update is incomplete.'
    }


    New-Item -ItemType Directory -Path $runnerPath | Out-Null
    foreach ($name in @('Update-WorkStack.ps1', 'Test-WorkStackSetup.ps1')) {
        $source = Join-Path $PSScriptRoot $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Installed update component is missing: $name"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $runnerPath $name)
    }

    Write-UpdateLog "Waiting for Work Stack process $ParentProcessId before applying $TargetVersion."
    if (Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue) {
        Wait-Process -Id $ParentProcessId -Timeout 90
    }

    New-InstallRecoverySnapshot
    $updater = Join-Path $runnerPath 'Update-WorkStack.ps1'
    # The transactional install never writes managed links: it is inside the
    # rollback-eligible scope, and a link written there would survive a rollback
    # pointing at a reverted payload. Finalization happens after the commit
    # boundary instead. This forced value must not reach the path policy above.
    & $updater -SetupPath $setup -ChecksumPath $checksum -InstallRoot $installPath -StateRoot $statePath -NoShortcut:$true
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Work Stack updater exited with code $LASTEXITCODE."
    }
    $installationApplied = $true

    $pythonw = Join-Path $installPath 'runtime\pythonw.exe'
    $entry = Join-Path $installPath 'desktop\python-webview-shell\workstack_desktop.py'
    if ([Environment]::GetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_LAUNCHER_VALIDATION', 'Process') -ceq '1') {
        throw 'Injected updated launcher validation failure.'
    }
    $desktopHost = Join-Path $installPath 'WorkStack.exe'
    if (-not (Test-Path -LiteralPath $desktopHost -PathType Leaf) -or
        -not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw 'The updated Work Stack desktop launcher is missing.'
    }
    # Same Windows argument encoding as the shortcuts, so the restart receives the
    # exact values even when a root legitimately ends in a separator.
    $arguments = @(
        (ConvertTo-WorkStackCommandLineArgument -Value $entry -AlwaysQuote),
        '--install-root',
        (ConvertTo-WorkStackCommandLineArgument -Value $installPath -AlwaysQuote),
        '--state-root',
        (ConvertTo-WorkStackCommandLineArgument -Value $statePath -AlwaysQuote)
    )
    if ([Environment]::GetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_RESTART', 'Process') -ceq '1') {
        throw 'Injected updated launcher restart failure.'
    }
    $restartProcess = Start-Process -FilePath $desktopHost -ArgumentList $arguments -WorkingDirectory $installPath -WindowStyle Hidden -PassThru
    if ($restartProcess.WaitForExit(1500)) {
        throw "The updated Work Stack launcher exited immediately with code $($restartProcess.ExitCode)."
    }
    # ---- COMMIT BOUNDARY -------------------------------------------------
    # The runtime started and did not exit within 1.5 seconds. That is an
    # acceptance signal, not a health check, but it is the point the update is
    # committed. Everything after this is derived state, so the rollback-eligible
    # scope ENDS here: nothing below may Restore, Stop the accepted process,
    # restore configuration or roll back payload.
    $updateCommitted = $true
} catch {
    $safeMessage = $_.Exception.Message
    if ($installationApplied) {
        try {
            Restore-InstallRecoverySnapshot
            Write-UpdateReceipt -Status 'rolled-back' -Message $safeMessage
            Write-UpdateLog "Update $TargetVersion failed after installation and was rolled back: $safeMessage"
        } catch {
            $recoveryMessage = "Update failed after installation and automatic rollback failed: $safeMessage; rollback error: $($_.Exception.Message)"
            Write-UpdateReceipt -Status 'recovery-required' -Message $recoveryMessage -RecoveryPath $recoveryPath
            Write-UpdateLog $recoveryMessage
        }
    } else {
        Write-UpdateReceipt -Status 'failed' -Message $safeMessage
        Write-UpdateLog "Update $TargetVersion failed before installation completed: $safeMessage"
    }
    exit 1
} finally {
    # Pre-commit only, which preserves the existing recovery semantics for an
    # uncommitted run. Once the update is committed the runner is derived
    # state: an unguarded failure here escaped the whole statement and took
    # the finalizer, the receipt, the log and exit 0 with it, so the committed
    # cleanup moves below into its own independently protected step.
    if (-not $updateCommitted -and (Test-Path -LiteralPath $runnerPath)) {
        Remove-Item -LiteralPath $runnerPath -Recurse -Force
    }
}

# ---- POST-COMMIT: derived state only ------------------------------------
# Reached only when the update committed. The rollback-eligible catch above is
# closed, so no failure here can Restore, Stop the accepted runtime, restore
# configuration or roll back payload. Every step is bounded best effort, one
# failure must not prevent the remaining safe reporting, and the process exits 0
# because the update genuinely is installed.
if (-not $updateCommitted) {
    # Defensive: the catch arms above always exit, so this is unreachable in
    # normal flow. Failing loudly beats silently reporting success.
    throw 'Post-commit finalization was reached without a committed update.'
}

$shortcutWarning = ''
if (-not $originalNoShortcut) {
    try {
        # The definitions loaded before the transaction are the PRE-UPDATE
        # ones: replacing files on disk does not redefine functions already
        # loaded in this process. The early load exists only so the
        # original-intent path guard can run before any mutation. What
        # finalizes after acceptance must be the helper that was just
        # installed, so it is re-loaded from the normalized installed
        # location here, inside this protected post-commit block, and only
        # then invoked. Nothing about Install is re-executed, and no claim
        # is made about an older already deployed Apply.
        $installedHelper = [IO.Path]::GetFullPath((Join-Path $installPath 'scripts\windows\WorkStack-Shortcuts.ps1'))
        if (-not (Test-Path -LiteralPath $installedHelper -PathType Leaf)) {
            throw "The installed shortcut helper is missing: $installedHelper"
        }
        . $installedHelper
        $finalization = Invoke-WorkStackShortcutFinalization -InstallPath $installPath -StatePath $statePath
        if (-not $finalization.Complete) {
            $shortcutWarning = Get-BoundedDiagnostic -Text $finalization.IncompleteReason
        }
    } catch {
        $shortcutWarning = Get-BoundedDiagnostic -Text $_.Exception.Message
    }
}

# Applied with warning: the status stays 'installed' and the receipt schema is
# unchanged. Only the message distinguishes a complete run from a derived
# failure, and the existing 500-character cap still applies.
$receiptMessage = if ($shortcutWarning) { "Shortcuts incomplete: $shortcutWarning" } else { '' }
$receiptWritten = Invoke-PostCommitStep -Description 'the update receipt could not be written' -Action {
    # Never claim a receipt persisted when its write failed.
    Write-UpdateReceipt -Status 'installed' -Message $receiptMessage
}
if ($shortcutWarning) {
    Invoke-PostCommitStep -Description 'its shortcuts are incomplete' -Action {
        Write-Warning "Work Stack $TargetVersion is installed and running, but its shortcuts are incomplete: $shortcutWarning"
    } | Out-Null
}

Invoke-PostCommitStep -Description 'the update log could not be written' -Action {
    if ($shortcutWarning) {
        Write-UpdateLog "Work Stack $TargetVersion installed and restarted successfully; shortcuts incomplete: $shortcutWarning"
    } else {
        Write-UpdateLog "Work Stack $TargetVersion installed and restarted successfully."
    }
    if (-not $receiptWritten) {
        Write-UpdateLog "Work Stack $TargetVersion installed, but the update receipt could not be written."
    }
} | Out-Null

Invoke-PostCommitStep -Description 'the successful update recovery snapshot could not be removed' -Action {
    if (Test-Path -LiteralPath $recoveryPath) {
        Remove-Item -LiteralPath $recoveryPath -Recurse -Force
    }
} | Out-Null

# The accepted runner cleanup, moved out of the rollback-eligible finally.
Invoke-PostCommitStep -Description 'the update runner directory could not be removed' -Action {
    if (Test-Path -LiteralPath $runnerPath) {
        Remove-Item -LiteralPath $runnerPath -Recurse -Force
    }
} | Out-Null

exit 0
