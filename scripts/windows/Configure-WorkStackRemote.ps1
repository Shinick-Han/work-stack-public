[CmdletBinding(DefaultParameterSetName = 'Remote')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Remote')]
    [ValidatePattern('^[A-Za-z0-9_.@-]{1,255}$')]
    [string]$SshHostAlias,
    [Parameter(Mandatory = $true, ParameterSetName = 'Remote')]
    [string]$RemoteAppDir,
    [Parameter(Mandatory = $true, ParameterSetName = 'Remote')]
    [string]$RemoteDataDir,
    [Parameter(Mandatory = $true, ParameterSetName = 'Remote')]
    [guid]$WorkspaceId,
    [Parameter(ParameterSetName = 'Remote')]
    [ValidateRange(1, 65535)]
    [int]$LocalForwardPort = 18765,
    [Parameter(ParameterSetName = 'Remote')]
    [ValidateRange(1, 65535)]
    [int]$RemotePort = 8765,
    [Parameter(Mandatory = $true, ParameterSetName = 'Local')]
    [switch]$UseLocal,
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$statePath = [IO.Path]::GetFullPath($StateRoot)
$profilePath = Join-Path $statePath 'remote-connection.json'

function Assert-LinuxAbsolutePath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not $Value.StartsWith('/') -or $Value.IndexOf([char]0) -ge 0 -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "$Name must be an absolute Linux path without control characters."
    }
    if ($Value.Split('/') | Where-Object { $_ -in '.', '..' }) {
        throw "$Name must not contain '.' or '..' path segments."
    }
}

New-Item -ItemType Directory -Force -Path $statePath | Out-Null
if ($UseLocal) {
    $profile = [ordered]@{ storage_mode = 'local' }
} else {
    Assert-LinuxAbsolutePath -Value $RemoteAppDir -Name 'RemoteAppDir'
    Assert-LinuxAbsolutePath -Value $RemoteDataDir -Name 'RemoteDataDir'
    $profile = [ordered]@{
        storage_mode = 'ssh-remote'
        ssh_host_alias = $SshHostAlias
        remote_app_dir = $RemoteAppDir.TrimEnd('/')
        remote_data_dir = $RemoteDataDir.TrimEnd('/')
        local_forward_port = $LocalForwardPort
        workspace_id = $WorkspaceId.ToString().ToLowerInvariant()
        remote_port = $RemotePort
    }
}

$encoded = $profile | ConvertTo-Json
$temporary = "$profilePath.tmp-$PID"
try {
    [IO.File]::WriteAllText($temporary, $encoded, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $profilePath -Force
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
}

Write-Host "Work Stack connection profile written to $profilePath"
Write-Host "No password, private key, token, or host-key bypass was stored."

if ($Check -and -not $UseLocal) {
    $configPath = Join-Path $statePath 'config.json'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Work Stack config is missing: $configPath"
    }
    $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    $installRoot = [IO.Path]::GetFullPath([string]$config.install_dir)
    $python = Join-Path $installRoot 'runtime\python.exe'
    $desktopEntry = Join-Path $installRoot 'desktop\python-webview-shell\workstack_desktop.py'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $desktopEntry -PathType Leaf)) {
        throw 'Installed Work Stack desktop runtime is missing.'
    }
    & $python $desktopEntry --state-root $statePath --install-root $installRoot --check-remote-connection
    if ($LASTEXITCODE -ne 0) {
        throw 'Work Stack SSH connection check failed.'
    }
}
