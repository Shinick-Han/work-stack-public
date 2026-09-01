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
    & $updater -SetupPath $setup -ChecksumPath $checksum -InstallRoot $installPath -StateRoot $statePath -NoShortcut:$NoShortcut
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Work Stack updater exited with code $LASTEXITCODE."
    }
    $installationApplied = $true

    $pythonw = Join-Path $installPath 'runtime\pythonw.exe'
    $entry = Join-Path $installPath 'desktop\python-webview-shell\workstack_desktop.py'
    if ([Environment]::GetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_LAUNCHER_VALIDATION', 'Process') -ceq '1') {
        throw 'Injected updated launcher validation failure.'
    }
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf) -or
        -not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw 'The updated Work Stack desktop launcher is missing.'
    }
    $arguments = @(
        ('"{0}"' -f $entry),
        '--install-root', ('"{0}"' -f $installPath),
        '--state-root', ('"{0}"' -f $statePath)
    )
    if ([Environment]::GetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_RESTART', 'Process') -ceq '1') {
        throw 'Injected updated launcher restart failure.'
    }
    $restartProcess = Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $installPath -WindowStyle Hidden -PassThru
    if ($restartProcess.WaitForExit(1500)) {
        throw "The updated Work Stack launcher exited immediately with code $($restartProcess.ExitCode)."
    }
    Write-UpdateReceipt -Status 'installed'
    Write-UpdateLog "Work Stack $TargetVersion installed and restarted successfully."
    try {
        if (Test-Path -LiteralPath $recoveryPath) {
            Remove-Item -LiteralPath $recoveryPath -Recurse -Force
        }
    } catch {
        Write-UpdateLog "The successful update recovery snapshot could not be removed: $($_.Exception.Message)"
    }
    exit 0
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
    if (Test-Path -LiteralPath $runnerPath) {
        Remove-Item -LiteralPath $runnerPath -Recurse -Force
    }
}
