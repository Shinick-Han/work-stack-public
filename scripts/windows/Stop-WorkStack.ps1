[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [int]$ProcessId = 0
)

$ErrorActionPreference = 'Stop'
$installPath = [IO.Path]::GetFullPath($InstallRoot)
$pythonPath = [IO.Path]::GetFullPath((Join-Path $installPath 'runtime\python.exe'))
$pythonwPath = [IO.Path]::GetFullPath((Join-Path $installPath 'runtime\pythonw.exe'))
$entryPath = [IO.Path]::GetFullPath((Join-Path $installPath 'run_work_stack.py'))
$desktopEntryPath = [IO.Path]::GetFullPath((Join-Path $installPath 'desktop\python-webview-shell\workstack_desktop.py'))
$stopped = 0
foreach ($candidate in Get-CimInstance Win32_Process) {
    if ($ProcessId -gt 0 -and [int]$candidate.ProcessId -ne $ProcessId) { continue }
    if (-not $candidate.ExecutablePath -or -not $candidate.CommandLine) { continue }
    $executable = [IO.Path]::GetFullPath([string]$candidate.ExecutablePath)
    $ownsServer =
        $executable.Equals($pythonPath, [StringComparison]::OrdinalIgnoreCase) -and
        ([string]$candidate.CommandLine).IndexOf($entryPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
    $ownsDesktop =
        $executable.Equals($pythonwPath, [StringComparison]::OrdinalIgnoreCase) -and
        ([string]$candidate.CommandLine).IndexOf($desktopEntryPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if ($ownsServer -or $ownsDesktop) {
        Stop-Process -Id ([int]$candidate.ProcessId) -Force
        $stopped++
    }
}
if ($stopped -gt 0) {
    Write-Host "Stopped $stopped Work Stack process(es)."
} else {
    Write-Host 'Work Stack is not running.'
}
