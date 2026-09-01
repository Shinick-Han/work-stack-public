[CmdletBinding()]
param(
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [ValidateRange(1, 10)]
    [int]$Samples = 3,
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'
$statePath = [IO.Path]::GetFullPath($StateRoot)
$profilePath = Join-Path $statePath 'remote-connection.json'
$configPath = Join-Path $statePath 'config.json'
if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
    throw "Remote SSH profile is missing: $profilePath"
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Work Stack config is missing: $configPath"
}

$profile = Get-Content -Raw -LiteralPath $profilePath | ConvertFrom-Json
if ($profile.storage_mode -ne 'ssh-remote') {
    throw 'The active Work Stack profile is not Remote SSH.'
}
if ([string]$profile.ssh_host_alias -notmatch '^[A-Za-z0-9_.@-]{1,255}$') {
    throw 'The saved SSH host alias is invalid.'
}

$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$installRoot = [IO.Path]::GetFullPath([string]$config.install_dir)
$python = Join-Path $installRoot 'runtime\python.exe'
$desktopEntry = Join-Path $installRoot 'desktop\python-webview-shell\workstack_desktop.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $desktopEntry -PathType Leaf)) {
    throw 'Installed Work Stack desktop runtime is missing.'
}

$productCheck = & $python $desktopEntry --state-root $statePath --install-root $installRoot --check-remote-connection
if ($LASTEXITCODE -ne 0) {
    throw 'The installed Work Stack remote profile check failed.'
}
$productStatus = $productCheck | Select-Object -Last 1 | ConvertFrom-Json
if ($productStatus.status -ne 'ready' -or $productStatus.storage_mode -ne 'ssh-remote') {
    throw 'The installed Work Stack remote profile did not report ready.'
}

$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$effectiveLines = & $ssh -G -- ([string]$profile.ssh_host_alias) 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'OpenSSH could not resolve the saved host alias.'
}
$effective = @{}
foreach ($line in $effectiveLines) {
    if ($line -match '^([^ ]+)\s+(.+)$') {
        $effective[$Matches[1].ToLowerInvariant()] = $Matches[2]
    }
}

$latencies = @()
for ($index = 0; $index -lt $Samples; $index += 1) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    & $ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -- ([string]$profile.ssh_host_alias) true 2>$null
    $exitCode = $LASTEXITCODE
    $timer.Stop()
    if ($exitCode -ne 0) {
        throw "Read-only SSH sample $($index + 1) failed. Review the host alias, agent, bastion, VPN, and known-host state."
    }
    $latencies += [int][Math]::Round($timer.Elapsed.TotalMilliseconds)
}

$sorted = @($latencies | Sort-Object)
$median = $sorted[[Math]::Floor(($sorted.Count - 1) / 2)]
$receipt = [ordered]@{
    schema_version = 1
    status = 'passed'
    checked_at = [DateTimeOffset]::UtcNow.ToString('o')
    storage_mode = 'ssh-remote'
    ssh_host_alias = [string]$profile.ssh_host_alias
    workspace_id = [string]$productStatus.workspace_id
    samples = $latencies
    median_milliseconds = $median
    effective_port = [int]$effective.port
    proxy_jump_configured = ($effective.ContainsKey('proxyjump') -and $effective.proxyjump -ne 'none') -or $effective.ContainsKey('proxycommand')
    strict_host_key_checking = [string]$effective.stricthostkeychecking
    identities_only = [string]$effective.identitiesonly
    server_alive_interval = [int]$effective.serveraliveinterval
    server_alive_count_max = [int]$effective.serveralivecountmax
}

if (-not $OutFile) {
    $diagnostics = Join-Path $statePath 'diagnostics'
    New-Item -ItemType Directory -Force -Path $diagnostics | Out-Null
    $stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $OutFile = Join-Path $diagnostics "ssh-network-$stamp.json"
}
$outputPath = [IO.Path]::GetFullPath($OutFile)
$outputParent = Split-Path -Parent $outputPath
New-Item -ItemType Directory -Force -Path $outputParent | Out-Null
[IO.File]::WriteAllText(
    $outputPath,
    (($receipt | ConvertTo-Json -Depth 4) + "`n"),
    [Text.UTF8Encoding]::new($false)
)
$receipt | ConvertTo-Json -Depth 4
Write-Host "Read-only SSH network receipt: $outputPath"
