[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [string]$ConfigPath = '',
    [switch]$NoBrowser,
    [switch]$SkipBackup,
    [string]$StatusPath = ''
)

$ErrorActionPreference = 'Stop'
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$stateRoot = [IO.Path]::GetFullPath($StateRoot)
$configPath = if ($ConfigPath) { [IO.Path]::GetFullPath($ConfigPath) } else { Join-Path $stateRoot 'config.json' }
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

function Test-LoopbackPortListening {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $client.Connect('127.0.0.1', $port)
        return $true
    } catch [Net.Sockets.SocketException] {
        return $false
    } finally {
        $client.Dispose()
    }
}

function ConvertTo-WindowsCommandLineArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Test-DriveAbsoluteOwnerConfigPath {
    param([string]$Text)
    if ($Text.Length -lt 3) { return $false }
    $letter = $Text[0]
    $isLetter = ($letter -ge 'A' -and $letter -le 'Z') -or ($letter -ge 'a' -and $letter -le 'z')
    if (-not $isLetter) { return $false }
    if ($Text[1] -ne ':') { return $false }
    return ($Text[2] -eq '\' -or $Text[2] -eq '/')
}

function Test-UncOwnerConfigBody {
    param([string]$Body)
    if ([string]::IsNullOrEmpty($Body) -or $Body.StartsWith('\') -or $Body.StartsWith('/')) {
        return $false
    }
    $parts = $Body.Replace('/', '\').Split('\')
    return ($parts.Length -ge 2 -and $parts[0].Length -gt 0 -and $parts[1].Length -gt 0)
}

function Test-WindowsAbsoluteOwnerConfigPath {
    param($Value)
    if ($Value -isnot [string] -or $Value.Length -eq 0) { return $false }
    for ($index = 0; $index -lt $Value.Length; $index++) {
        $code = [int][char]$Value[$index]
        if ($code -lt 32 -or $code -eq 127) { return $false }
    }
    if ($Value.StartsWith('\\?\')) {
        $rest = $Value.Substring(4)
        if ($rest.Length -ge 4 -and $rest.Substring(0, 4).ToUpperInvariant() -eq 'UNC\') {
            return Test-UncOwnerConfigBody $rest.Substring(4)
        }
        return Test-DriveAbsoluteOwnerConfigPath $rest
    }
    if ($Value.StartsWith('\\')) {
        return Test-UncOwnerConfigBody $Value.Substring(2)
    }
    return Test-DriveAbsoluteOwnerConfigPath $Value
}

function Resolve-KnowledgeDriversConfigArguments {
    param([Parameter(Mandatory = $true)]$Config)
    $property = $Config.PSObject.Properties |
        Where-Object { $_.Name -ceq 'knowledge_drivers_config' } |
        Select-Object -First 1
    if ($null -eq $property) { return [string[]]@() }
    $raw = $property.Value
    if (-not (Test-WindowsAbsoluteOwnerConfigPath -Value $raw)) {
        throw "Work Stack knowledge drivers configuration path is invalid."
    }
    return [string[]]@(
        '--knowledge-drivers-config'
        (ConvertTo-WindowsCommandLineArgument ([string]$raw))
    )
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
if (Test-LoopbackPortListening) {
    throw "Configured port $port is already in use by a non-Work Stack process. Re-run the installer to select an available port."
}

$knowledgeDriversArguments = @(Resolve-KnowledgeDriversConfigArguments -Config $config)

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
$argumentTokens = @(
    (ConvertTo-WindowsCommandLineArgument $entryPath)
    '--data-dir'
    (ConvertTo-WindowsCommandLineArgument $dataPath)
    'graph'
    'serve'
    '--host'
    '127.0.0.1'
    '--port'
    [string]$port
) + $knowledgeDriversArguments
$arguments = $argumentTokens -join ' '
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
