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
$hostPath = [IO.Path]::GetFullPath((Join-Path $installPath 'WorkStack.exe'))

function Split-WorkStackCommandLine {
    <#
        Parse a Windows command line into argv, honouring quoted, spaced and
        Unicode paths. A malformed or ambiguous line yields $null so the caller
        refuses ownership rather than guessing. Nothing here evaluates the line.
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$CommandLine)

    $arguments = New-Object System.Collections.Generic.List[string]
    $current = New-Object System.Text.StringBuilder
    $inQuotes = $false
    $started = $false
    $backslashes = 0

    foreach ($character in $CommandLine.ToCharArray()) {
        if ($character -eq '\') { $backslashes++; continue }
        if ($character -eq '"') {
            [void]$current.Append('\', [int][Math]::Floor($backslashes / 2))
            if ($backslashes % 2 -eq 1) { [void]$current.Append('"') }
            else { $inQuotes = -not $inQuotes }
            $backslashes = 0
            $started = $true
            continue
        }
        [void]$current.Append('\', $backslashes)
        $backslashes = 0
        if (-not $inQuotes -and [char]::IsWhiteSpace($character)) {
            if ($started) { [void]$arguments.Add($current.ToString()); [void]$current.Clear(); $started = $false }
            continue
        }
        [void]$current.Append($character)
        $started = $true
    }
    [void]$current.Append('\', $backslashes)
    if ($inQuotes) { return $null }
    if ($started -or $current.Length -gt 0) { [void]$arguments.Add($current.ToString()) }
    return , $arguments.ToArray()
}

function Test-WorkStackAbsolutePath {
    <#
        A real absolute Windows path domain, not a prefix test: a drive or UNC
        root, no invalid path characters and no wildcard, so a lowercase but
        malformed value such as C:\bad|path is refused before ownership.
    #>
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    if (-not ($Value -match '^(?:[A-Za-z]:[\\/]|\\\\)')) { return $false }
    foreach ($invalid in [IO.Path]::GetInvalidPathChars()) {
        if ($Value.IndexOf($invalid) -ge 0) { return $false }
    }
    if ($Value.IndexOfAny([char[]]@('|', '?', '*', '<', '>', '"')) -ge 0) { return $false }
    try { $null = [IO.Path]::GetFullPath($Value) } catch { return $false }
    return $true
}

function Test-WorkStackIntegerValue {
    <#
        The shape CPython 3.12.10 int() accepts, using that interpreter's own
        decimal-scalar table (unicodedata 15.0.0) rather than the framework's
        older UTF-16 char classification, which cannot see supplementary digits
        such as Adlam U+1E950 or Mathematical U+1D7CE. Never converted.
    #>
    param([string]$Value)
    if ($null -eq $Value) { return $false }
    $ranges = @(@(0x30,0x39),@(0x660,0x669),@(0x6F0,0x6F9),@(0x7C0,0x7C9),@(0x966,0x96F),@(0x9E6,0x9EF),@(0xA66,0xA6F),@(0xAE6,0xAEF),@(0xB66,0xB6F),@(0xBE6,0xBEF),@(0xC66,0xC6F),@(0xCE6,0xCEF),@(0xD66,0xD6F),@(0xDE6,0xDEF),@(0xE50,0xE59),@(0xED0,0xED9),@(0xF20,0xF29),@(0x1040,0x1049),@(0x1090,0x1099),@(0x17E0,0x17E9),@(0x1810,0x1819),@(0x1946,0x194F),@(0x19D0,0x19D9),@(0x1A80,0x1A89),@(0x1A90,0x1A99),@(0x1B50,0x1B59),@(0x1BB0,0x1BB9),@(0x1C40,0x1C49),@(0x1C50,0x1C59),@(0xA620,0xA629),@(0xA8D0,0xA8D9),@(0xA900,0xA909),@(0xA9D0,0xA9D9),@(0xA9F0,0xA9F9),@(0xAA50,0xAA59),@(0xABF0,0xABF9),@(0xFF10,0xFF19),@(0x104A0,0x104A9),@(0x10D30,0x10D39),@(0x11066,0x1106F),@(0x110F0,0x110F9),@(0x11136,0x1113F),@(0x111D0,0x111D9),@(0x112F0,0x112F9),@(0x11450,0x11459),@(0x114D0,0x114D9),@(0x11650,0x11659),@(0x116C0,0x116C9),@(0x11730,0x11739),@(0x118E0,0x118E9),@(0x11950,0x11959),@(0x11C50,0x11C59),@(0x11D50,0x11D59),@(0x11DA0,0x11DA9),@(0x11F50,0x11F59),@(0x16A60,0x16A69),@(0x16AC0,0x16AC9),@(0x16B50,0x16B59),@(0x1D7CE,0x1D7FF),@(0x1E140,0x1E149),@(0x1E2F0,0x1E2F9),@(0x1E4F0,0x1E4F9),@(0x1E950,0x1E959),@(0x1FBF0,0x1FBF9))
    $trimmed = $Value.Trim()
    if ($trimmed.Length -eq 0) { return $false }
    $index = 0
    if ($trimmed[0] -eq '+' -or $trimmed[0] -eq '-') { $index = 1 }
    $previousWasDigit = $false
    $sawDigit = $false
    while ($index -lt $trimmed.Length) {
        $current = $trimmed[$index]
        if ($current -eq '_') {
            if (-not $previousWasDigit) { return $false }
            $previousWasDigit = $false
            $index++
            continue
        }
        if ([char]::IsLowSurrogate($current)) { return $false }
        if ([char]::IsHighSurrogate($current)) {
            if ($index + 1 -ge $trimmed.Length -or -not [char]::IsLowSurrogate($trimmed[$index + 1])) { return $false }
            $scalar = [char]::ConvertToUtf32($trimmed, $index)
            $index += 2
        } else {
            $scalar = [int]$current
            $index++
        }
        $isDecimal = $false
        foreach ($range in $ranges) {
            if ($scalar -ge $range[0] -and $scalar -le $range[1]) { $isDecimal = $true; break }
        }
        if (-not $isDecimal) { return $false }
        $previousWasDigit = $true
        $sawDigit = $true
    }
    return ($sawDigit -and $previousWasDigit)
}

function Test-WorkStackDesktopGrammar {
    <#
        Bind ownership to the COMPLETE supported desktop invocation, not to the
        image plus a single position. Every remaining argument must belong to the
        frozen option grammar, paths are compared as fully absolute semantic paths
        with no working-directory resolution, and an option value such as --url can
        never supply script identity.

        The branded host accepts the desktop script optionally, including the valid
        zero-user-argument form. The legacy pythonw invocation requires the script
        in its exact position, as the installed shortcuts have always written it.
    #>
    param(
        [string[]]$Argv,
        [Parameter(Mandatory = $true)][string]$ExpectedEntry,
        [Parameter(Mandatory = $true)][string]$ExpectedInstallRoot,
        [switch]$ScriptRequired
    )

    if ($null -eq $Argv) { return $false }
    $index = 1
    if ($Argv.Count -gt 1 -and -not ($Argv[1]).StartsWith('--')) {
        if (-not (Test-WorkStackExactPath -Actual $Argv[1] -Expected $ExpectedEntry)) { return $false }
        $index = 2
    } elseif ($ScriptRequired) {
        return $false
    }

    # Exact case-sensitive spelling, and each value is checked with the same
    # meaning the host itself enforces, so no option can be swallowed as a value.
    $allowed = @('--install-root', '--state-root', '--url', '--probe-provider', '--probe-result', '--auto-close-seconds')
    $providers = @('outlook', 'teams', 'onenote')
    $seen = @{}
    while ($index -lt $Argv.Count) {
        $option = $Argv[$index]
        if ($allowed -cnotcontains $option) { return $false }
        if ($seen.ContainsKey($option)) { return $false }
        $seen[$option] = $true
        if ($index + 1 -ge $Argv.Count) { return $false }
        $value = $Argv[$index + 1]
        # An option name in the value slot means the previous option had none.
        if ($allowed -ccontains $value) { return $false }
        $matched = $true
        switch -CaseSensitive ($option) {
            '--install-root' {
                if (-not (Test-WorkStackExactPath -Actual $value -Expected $ExpectedInstallRoot)) { return $false }
            }
            '--state-root' {
                if (-not (Test-WorkStackAbsolutePath -Value $value)) { return $false }
            }
            '--probe-result' {
                if (-not (Test-WorkStackAbsolutePath -Value $value)) { return $false }
            }
            '--probe-provider' {
                if ($providers -cnotcontains $value) { return $false }
            }
            '--auto-close-seconds' {
                if (-not (Test-WorkStackIntegerValue -Value $value)) { return $false }
            }
            '--url' { }
            default { $matched = $false }
        }
        # An option that reached no case is not admitted by silence.
        if (-not $matched) { return $false }
        $index += 2
    }
    return $true
}

function Test-WorkStackExactPath {
    <#
        Compare one argument against an expected path as fully absolute semantic
        paths. A relative value is refused rather than resolved against whatever
        working directory the caller happens to have.
    #>
    param(
        [string]$Actual,
        [Parameter(Mandatory = $true)][string]$Expected
    )
    if ([string]::IsNullOrWhiteSpace($Actual)) { return $false }
    if (-not ($Actual -match '^(?:[A-Za-z]:[\\/]|\\\\)')) { return $false }
    try { $resolved = [IO.Path]::GetFullPath($Actual) } catch { return $false }
    $left = $resolved.TrimEnd([IO.Path]::DirectorySeparatorChar)
    $right = ([IO.Path]::GetFullPath($Expected)).TrimEnd([IO.Path]::DirectorySeparatorChar)
    return [string]::Equals($left, $right, [StringComparison]::OrdinalIgnoreCase)
}

function Test-WorkStackDesktopInvocation {
    <#
        True only when argv[0] is the expected image AND the complete remaining
        invocation satisfies the frozen desktop grammar.
    #>
    param(
        [string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$ExpectedImage,
        [Parameter(Mandatory = $true)][string]$ExpectedEntry,
        [Parameter(Mandatory = $true)][string]$ExpectedInstallRoot,
        [switch]$ScriptRequired
    )
    $argv = Split-WorkStackCommandLine -CommandLine $CommandLine
    if ($null -eq $argv -or $argv.Count -lt 1) { return $false }
    if (-not (Test-WorkStackExactPath -Actual $argv[0] -Expected $ExpectedImage)) { return $false }
    return Test-WorkStackDesktopGrammar -Argv $argv -ExpectedEntry $ExpectedEntry `
        -ExpectedInstallRoot $ExpectedInstallRoot -ScriptRequired:$ScriptRequired
}

$stopped = 0
foreach ($candidate in Get-CimInstance Win32_Process) {
    if ($ProcessId -gt 0 -and [int]$candidate.ProcessId -ne $ProcessId) { continue }
    if (-not $candidate.ExecutablePath -or -not $candidate.CommandLine) { continue }
    $executable = [IO.Path]::GetFullPath([string]$candidate.ExecutablePath)
    $ownsServer =
        $executable.Equals($pythonPath, [StringComparison]::OrdinalIgnoreCase) -and
        ([string]$candidate.CommandLine).IndexOf($entryPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
    # The branded host and the legacy bundled pythonw.exe are both recognised, but
    # only with the exact installed desktop entry in its semantic argv position.
    # The branded host may omit the redundant script; the legacy launcher must
    # carry it in its exact position, as the installed shortcuts always wrote it.
    $ownsDesktopHost =
        $executable.Equals($hostPath, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-WorkStackDesktopInvocation -CommandLine ([string]$candidate.CommandLine) -ExpectedImage $hostPath -ExpectedEntry $desktopEntryPath -ExpectedInstallRoot $installPath)
    $ownsDesktopLegacy =
        $executable.Equals($pythonwPath, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-WorkStackDesktopInvocation -CommandLine ([string]$candidate.CommandLine) -ExpectedImage $pythonwPath -ExpectedEntry $desktopEntryPath -ExpectedInstallRoot $installPath -ScriptRequired)
    $ownsDesktop = $ownsDesktopHost -or $ownsDesktopLegacy
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
