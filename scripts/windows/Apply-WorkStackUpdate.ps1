[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [Parameter(Mandatory = $true)][string]$ChecksumPath,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][ValidateRange(1, 2147483647)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][ValidatePattern('^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$')][string]$TargetVersion
)

$ErrorActionPreference = 'Stop'
$setup = [IO.Path]::GetFullPath($SetupPath)
$checksum = [IO.Path]::GetFullPath($ChecksumPath)
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$statePath = [IO.Path]::GetFullPath($StateRoot)
$updatesPath = Join-Path $statePath 'updates'
$runnerPath = Join-Path $updatesPath ".apply-$TargetVersion-$PID"
$receiptPath = Join-Path $updatesPath 'last-update.json'
$logPath = Join-Path $statePath 'logs\desktop-update.log'

New-Item -ItemType Directory -Force -Path $updatesPath, (Split-Path -Parent $logPath) | Out-Null

function Write-UpdateReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Message = ''
    )
    $receipt = [ordered]@{
        schema_version = 1
        version = $TargetVersion
        status = $Status
        recorded_at = [DateTimeOffset]::UtcNow.ToString('o')
        message = $Message.Substring(0, [Math]::Min($Message.Length, 500))
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

    $updater = Join-Path $runnerPath 'Update-WorkStack.ps1'
    & $updater -SetupPath $setup -ChecksumPath $checksum -InstallRoot $installPath -StateRoot $statePath
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Work Stack updater exited with code $LASTEXITCODE."
    }

    Write-UpdateReceipt -Status 'installed'
    Write-UpdateLog "Work Stack $TargetVersion installed successfully."

    $pythonw = Join-Path $installPath 'runtime\pythonw.exe'
    $entry = Join-Path $installPath 'desktop\python-webview-shell\workstack_desktop.py'
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf) -or
        -not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw 'The updated Work Stack desktop launcher is missing.'
    }
    $arguments = @(
        ('"{0}"' -f $entry),
        '--install-root', ('"{0}"' -f $installPath),
        '--state-root', ('"{0}"' -f $statePath)
    )
    Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $installPath -WindowStyle Hidden
    exit 0
} catch {
    $safeMessage = $_.Exception.Message
    Write-UpdateReceipt -Status 'failed' -Message $safeMessage
    Write-UpdateLog "Update $TargetVersion failed: $safeMessage"
    exit 1
} finally {
    if (Test-Path -LiteralPath $runnerPath) {
        Remove-Item -LiteralPath $runnerPath -Recurse -Force
    }
}
