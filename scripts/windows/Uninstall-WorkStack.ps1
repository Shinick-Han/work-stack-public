[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [switch]$RemoveData
)

$ErrorActionPreference = 'Stop'
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$localPrograms = [IO.Path]::GetFullPath("$env:LOCALAPPDATA\Programs")
if (-not $installPath.StartsWith($localPrograms + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to uninstall outside LOCALAPPDATA\Programs.'
}
$stopScript = Join-Path $installPath 'scripts\windows\Stop-WorkStack.ps1'
if (Test-Path -LiteralPath $stopScript) { & $stopScript -InstallRoot $installPath }
foreach ($shortcutName in @('Work Stack.lnk', 'Work Stack Maintenance.lnk')) {
    $shortcutPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$shortcutName"
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath -Force }
}
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Work Stack.lnk'
if (Test-Path -LiteralPath $desktopShortcut) { Remove-Item -LiteralPath $desktopShortcut -Force }
if (Test-Path -LiteralPath $installPath) { Remove-Item -LiteralPath $installPath -Recurse -Force }
if ($RemoveData) {
    $stateRoot = [IO.Path]::GetFullPath($StateRoot)
    if (-not $stateRoot.StartsWith([IO.Path]::GetFullPath($env:LOCALAPPDATA) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to remove data outside LOCALAPPDATA.'
    }
    Remove-Item -LiteralPath $stateRoot -Recurse -Force
    Write-Host 'Work Stack and local planning data were removed.'
} else {
    Write-Host "Work Stack was removed. Planning data and backups remain under $StateRoot."
}
