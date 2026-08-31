[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [switch]$NoBrowser,
    [switch]$SkipBackup,
    [string]$StatusPath = ''
)

$ErrorActionPreference = 'Stop'
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$stateRoot = [IO.Path]::GetFullPath($StateRoot)
$configPath = Join-Path $stateRoot 'config.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Work Stack is not configured. Run Install-WorkStack.ps1 first."
}
$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$dataPath = [IO.Path]::GetFullPath([string]$config.data_dir)
$backupPath = [IO.Path]::GetFullPath([string]$config.backup_dir)
$logPath = Join-Path $stateRoot 'logs'
$pythonPath = Join-Path $installPath 'runtime\python.exe'
$entryPath = Join-Path $installPath 'run_work_stack.py'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf) -or -not (Test-Path -LiteralPath $entryPath -PathType Leaf)) {
    throw "Work Stack installation is incomplete. Re-run the installer."
}

$port = [int]$config.port
$url = "http://127.0.0.1:$port/"

function Write-LaunchStatus {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('started', 'reused')][string]$Status,
        [AllowNull()][Nullable[int]]$ServerProcessId
    )
    if (-not $StatusPath) { return }
    $resolvedStatus = [IO.Path]::GetFullPath($StatusPath)
    $statusParent = Split-Path -Parent $resolvedStatus
    New-Item -ItemType Directory -Force -Path $statusParent | Out-Null
    $temporaryStatus = "$resolvedStatus.tmp-$PID"
    @{
        status = $Status
        pid = $ServerProcessId
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporaryStatus -Encoding utf8
    Move-Item -LiteralPath $temporaryStatus -Destination $resolvedStatus -Force
}

function Test-WorkStackReady {
    try {
        $healthUrl = "$($url.TrimEnd('/'))/api/v1/health"
        $health = Invoke-RestMethod -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        return (
            $null -ne $health.data -and
            [string]$health.data.api_version -eq 'v1' -and
            [string]$health.data.status -eq 'ready'
        )
    } catch {
        return $false
    }
}

function ConvertTo-WindowsCommandLineArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Open-WorkStackBrowser {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ProfileRoot
    )

    $browserCandidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
    )
    $browserArguments = @(
        (ConvertTo-WindowsCommandLineArgument "--app=$Url")
        (ConvertTo-WindowsCommandLineArgument "--user-data-dir=$ProfileRoot")
        '--start-maximized'
    ) -join ' '
    foreach ($candidate in $browserCandidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            Start-Process -FilePath $candidate -ArgumentList $browserArguments
            return
        }
    }
    Start-Process $Url
}

$browserProfilePath = Join-Path $stateRoot 'browser-profile'
if (Test-WorkStackReady) {
    Write-LaunchStatus -Status 'reused' -ServerProcessId $null
    if (-not $NoBrowser) { Open-WorkStackBrowser -Url $url -ProfileRoot $browserProfilePath }
    Write-Host "Work Stack is already running at $url"
    exit 0
}

New-Item -ItemType Directory -Force -Path $dataPath, $backupPath, $logPath | Out-Null
if (-not $SkipBackup -and (Test-Path -LiteralPath (Join-Path $dataPath 'workspace.json'))) {
    & $pythonPath $entryPath --data-dir $dataPath maintenance backup --out $backupPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Automatic pre-launch backup failed; Work Stack was not started." }
    $retention = [Math]::Max(1, [int]$config.backup_retention)
    $expired = Get-ChildItem -LiteralPath $backupPath -Filter 'workstack-backup-*.zip' -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $retention
    foreach ($item in $expired) {
        $resolved = [IO.Path]::GetFullPath($item.FullName)
        if (-not $resolved.StartsWith($backupPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to prune a backup outside the configured backup directory."
        }
        Remove-Item -LiteralPath $resolved -Force
    }
}

$stdout = Join-Path $logPath 'server.out.log'
$stderr = Join-Path $logPath 'server.err.log'
$arguments = @(
    (ConvertTo-WindowsCommandLineArgument $entryPath)
    '--data-dir'
    (ConvertTo-WindowsCommandLineArgument $dataPath)
    'graph'
    'serve'
    '--host'
    '127.0.0.1'
    '--port'
    [string]$port
) -join ' '
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if ($process.HasExited) { break }
    if (Test-WorkStackReady) { $ready = $true; break }
    Start-Sleep -Milliseconds 250
}
if (-not $ready) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    throw "Work Stack did not start. Inspect $stderr"
}
Write-LaunchStatus -Status 'started' -ServerProcessId $process.Id
if (-not $NoBrowser) { Open-WorkStackBrowser -Url $url -ProfileRoot $browserProfilePath }
Write-Host "Work Stack is running at $url (PID $($process.Id))."
