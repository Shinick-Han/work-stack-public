[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath,
    [string]$ChecksumPath = '',
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'
$setup = [IO.Path]::GetFullPath($SetupPath)
if (-not (Test-Path -LiteralPath $setup -PathType Leaf) -or [IO.Path]::GetExtension($setup) -ne '.ps1') {
    throw 'SetupPath must name a downloaded Work Stack PowerShell setup artifact.'
}
$verifier = Join-Path $PSScriptRoot 'Test-WorkStackSetup.ps1'
if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
    throw 'Installed Work Stack setup verifier is missing.'
}
& $verifier -SetupPath $setup -ChecksumPath $ChecksumPath | Out-Host
$statePath = [IO.Path]::GetFullPath($StateRoot)
$configPath = Join-Path $statePath 'config.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Work Stack is not configured. Install it before updating.'
}
$config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$dataPath = [IO.Path]::GetFullPath([string]$config.data_dir)
$backupPath = [IO.Path]::GetFullPath([string]$config.backup_dir)
$port = [int]$config.port
$backupRetention = [Math]::Max(1, [int]$config.backup_retention)
& $setup -InstallRoot ([IO.Path]::GetFullPath($InstallRoot)) -StateRoot $statePath -DataDir $dataPath -BackupDir $backupPath -Port $port -BackupRetention $backupRetention -NoShortcut:$NoShortcut
