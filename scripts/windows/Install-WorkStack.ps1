[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [string]$DataDir = "$env:LOCALAPPDATA\WorkStack\data",
    [int]$Port = 8765,
    [int]$BackupRetention = 14,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'
if (-not $SourceRoot) { $SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
$sourcePath = [IO.Path]::GetFullPath($SourceRoot)
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$statePath = [IO.Path]::GetFullPath($StateRoot)
$dataPath = [IO.Path]::GetFullPath($DataDir)
$backupRoot = [IO.Path]::GetFullPath((Join-Path $statePath 'backups'))

function Assert-PathsDisjoint {
    param(
        [Parameter(Mandatory = $true)][string]$First,
        [Parameter(Mandatory = $true)][string]$Second,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $separator = [IO.Path]::DirectorySeparatorChar
    $firstPath = [IO.Path]::GetFullPath($First).TrimEnd($separator)
    $secondPath = [IO.Path]::GetFullPath($Second).TrimEnd($separator)
    $firstContainsSecond = $secondPath.Equals($firstPath, [StringComparison]::OrdinalIgnoreCase) -or
        $secondPath.StartsWith($firstPath + $separator, [StringComparison]::OrdinalIgnoreCase)
    $secondContainsFirst = $firstPath.StartsWith($secondPath + $separator, [StringComparison]::OrdinalIgnoreCase)
    if ($firstContainsSecond -or $secondContainsFirst) {
        throw "Unsafe path overlap ($Description): '$firstPath' and '$secondPath'."
    }
}

function New-WorkStackIcon {
    param([Parameter(Mandatory = $true)][string]$Path)
    Add-Type -AssemblyName System.Drawing
    $bitmap = New-Object System.Drawing.Bitmap 64, 64
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $shape = New-Object System.Drawing.Drawing2D.GraphicsPath
    $accent = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(184, 242, 75))
    $ink = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(25, 34, 16))
    $icon = $null
    $stream = $null
    try {
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $shape.AddArc(4, 4, 16, 16, 180, 90)
        $shape.AddArc(44, 4, 16, 16, 270, 90)
        $shape.AddArc(44, 44, 16, 16, 0, 90)
        $shape.AddArc(4, 44, 16, 16, 90, 90)
        $shape.CloseFigure()
        $graphics.FillPath($accent, $shape)
        $graphics.FillRectangle($ink, 18, 22, 7, 20)
        $graphics.FillRectangle($ink, 29, 15, 7, 34)
        $graphics.FillRectangle($ink, 40, 20, 7, 25)
        $icon = [System.Drawing.Icon]::FromHandle($bitmap.GetHicon()).Clone()
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $icon.Save($stream)
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($icon) { $icon.Dispose() }
        $ink.Dispose()
        $accent.Dispose()
        $shape.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

Assert-PathsDisjoint -First $installPath -Second $statePath -Description 'install/state'
Assert-PathsDisjoint -First $installPath -Second $dataPath -Description 'install/data'
Assert-PathsDisjoint -First $installPath -Second $backupRoot -Description 'install/backup'
Assert-PathsDisjoint -First $dataPath -Second $backupRoot -Description 'data/backup'

$localPrograms = [IO.Path]::GetFullPath("$env:LOCALAPPDATA\Programs")
if (-not $installPath.StartsWith($localPrograms + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and -not $NoShortcut) {
    throw 'The default interactive installer only writes under LOCALAPPDATA\Programs.'
}
foreach ($required in @('workstack', 'frontend\dist', 'run_work_stack.py', 'requirements.txt', 'requirements-windows-desktop.txt', 'scripts\windows', 'desktop\python-webview-shell\workstack_desktop.py', 'runtime\python.exe', 'runtime\pythonw.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourcePath $required))) { throw "Installer source is missing $required" }
}
$parent = Split-Path -Parent $installPath
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$staging = [IO.Path]::GetFullPath("$installPath.staging-$PID")
$rollback = [IO.Path]::GetFullPath("$installPath.rollback-$PID")
if (Test-Path -LiteralPath $staging) { throw "Staging path already exists: $staging" }
New-Item -ItemType Directory -Path $staging | Out-Null

try {
    foreach ($directory in @('workstack', 'contracts', 'licenses', 'web', 'runtime', 'desktop')) {
        Copy-Item -LiteralPath (Join-Path $sourcePath $directory) -Destination (Join-Path $staging $directory) -Recurse
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $staging 'frontend'), (Join-Path $staging 'scripts') | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourcePath 'frontend\dist') -Destination (Join-Path $staging 'frontend\dist') -Recurse
    Copy-Item -LiteralPath (Join-Path $sourcePath 'scripts\windows') -Destination (Join-Path $staging 'scripts\windows') -Recurse
    foreach ($file in @('run_work_stack.py', 'requirements.txt', 'requirements-windows-desktop.txt', 'README.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md')) {
        Copy-Item -LiteralPath (Join-Path $sourcePath $file) -Destination (Join-Path $staging $file)
    }

    $stagedPython = Join-Path $staging 'runtime\python.exe'
    $pythonTarget = & $stagedPython -c "import struct,sys,unicodedata2,workstack,webview; from webview.platforms.edgechromium import WebView2; assert unicodedata2.unidata_version == '17.0.0'; print(f'{sys.version_info.major}.{sys.version_info.minor}:{struct.calcsize(chr(80))*8}')" 2>$null
    if ($LASTEXITCODE -ne 0 -or $pythonTarget.Trim() -ne '3.12:64') {
        throw 'Bundled 64-bit Python 3.12 runtime smoke test failed.'
    }

    if (Test-Path -LiteralPath $installPath) {
        $stopScript = Join-Path $staging 'scripts\windows\Stop-WorkStack.ps1'
        if (Test-Path -LiteralPath $stopScript) { & $stopScript -InstallRoot $installPath }
        $installedPython = Join-Path $installPath 'runtime\python.exe'
        if (-not (Test-Path -LiteralPath $installedPython -PathType Leaf)) {
            # One-time compatibility for upgrades from the earlier venv-based prototype.
            $legacyPython = Join-Path $installPath '.venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $legacyPython -PathType Leaf) { $installedPython = $legacyPython }
        }
        $installedEntry = Join-Path $installPath 'run_work_stack.py'
        if ((Test-Path -LiteralPath (Join-Path $dataPath 'workspace.json')) -and (Test-Path -LiteralPath $installedPython)) {
            & $installedPython $installedEntry --data-dir $dataPath maintenance backup --out $backupRoot | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Pre-upgrade backup failed; installation was not changed.' }
        }
        Move-Item -LiteralPath $installPath -Destination $rollback
    }
    Move-Item -LiteralPath $staging -Destination $installPath

    New-Item -ItemType Directory -Force -Path $statePath, $dataPath, $backupRoot | Out-Null
    $configJson = @{
        version = 1
        install_dir = $installPath
        data_dir = $dataPath
        backup_dir = $backupRoot
        backup_retention = [Math]::Max(1, $BackupRetention)
        port = $Port
    } | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $statePath 'config.json'), $configJson, [Text.UTF8Encoding]::new($false))

    if (-not $NoShortcut) {
        $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
        $shell = New-Object -ComObject WScript.Shell
        $desktopPython = Join-Path $installPath 'runtime\pythonw.exe'
        $desktopEntry = Join-Path $installPath 'desktop\python-webview-shell\workstack_desktop.py'
        $iconPath = Join-Path $installPath 'WorkStack.ico'
        New-WorkStackIcon -Path $iconPath
        $desktopFolder = [Environment]::GetFolderPath('Desktop')
        foreach ($shortcutPath in @(
            (Join-Path $startMenu 'Work Stack.lnk'),
            (Join-Path $desktopFolder 'Work Stack.lnk')
        )) {
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = $desktopPython
            $shortcut.Arguments = "`"$desktopEntry`" --install-root `"$installPath`" --state-root `"$statePath`""
            $shortcut.WorkingDirectory = $installPath
            $shortcut.IconLocation = "$iconPath,0"
            $shortcut.Save()
        }

        $maintenanceShortcutPath = Join-Path $startMenu 'Work Stack Maintenance.lnk'
        $maintenanceShortcut = $shell.CreateShortcut($maintenanceShortcutPath)
        $maintenanceShortcut.TargetPath = 'powershell.exe'
        $maintenanceShortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $installPath 'scripts\windows\Maintain-WorkStack.ps1')`" -InstallRoot `"$installPath`" -StateRoot `"$statePath`""
        $maintenanceShortcut.WorkingDirectory = $installPath
        $maintenanceShortcut.Save()
    }
    if (Test-Path -LiteralPath $rollback) { Remove-Item -LiteralPath $rollback -Recurse -Force }
    Write-Host "Work Stack installed at $installPath"
    Write-Host "Planning data remains at $dataPath"
} catch {
    if (Test-Path -LiteralPath $rollback) {
        if (Test-Path -LiteralPath $installPath) {
            Remove-Item -LiteralPath $installPath -Recurse -Force
        }
        Move-Item -LiteralPath $rollback -Destination $installPath
    }
    throw
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
