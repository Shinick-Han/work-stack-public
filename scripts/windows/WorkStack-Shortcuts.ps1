# Work Stack managed-shortcut finalization.
#
# FUNCTIONS ONLY. Dot-sourcing this file must have no effect whatsoever: no I/O,
# no COM, no Add-Type, no process start, no Store or configuration access and no
# installer effect. Everything below is a definition; nothing runs at load. The
# native notification support is created lazily, inside the function that needs
# it, for the same reason.
#
# The finalizer runs AFTER the update's commit boundary, so it is derived state.
# If it fails, the installed runtime stays installed and running: the caller
# reports "applied with warning" and exits 0 rather than rolling anything back.
# There is deliberately no shortcut journal, no byte snapshot and no
# compare-and-delete ownership, so links may be stale, absent or partially
# rewritten while the runtime is healthy. That limitation is accepted policy,
# not an oversight.

function ConvertTo-WorkStackCommandLineArgument {
    <#
        Windows argument encoding: quote when needed, escape embedded quotes and
        double the backslashes that precede a closing quote so a trailing
        separator survives the round trip.
    #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value,
        [switch]$AlwaysQuote
    )

    if (-not $AlwaysQuote -and $Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Get-WorkStackShortcutNotificationConstant {
    <#
        .SYNOPSIS
        The SHChangeNotify constants this file uses.

        .DESCRIPTION
        Returned from a function rather than assigned at file scope, so
        dot-sourcing this file defines names and changes nothing else. Not even
        Set-StrictMode is set here: altering the caller's session would itself be
        a load-time effect.

        Values follow shlobj_core semantics documented at
        https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shchangenotify
        MaxNotifyPath is the API's MAX_PATH path-input limit. MAX_PATH counts
        the terminating NUL, so at most 259 non-NUL characters fit this input
        contract: 259 is announceable, 260 and above are not. A path that does
        not fit is reported derived-incomplete rather than claimed as a
        refresh.
    #>

    return [pscustomobject]@{
        ShcneCreate = 0x00000002      # SHCNE_CREATE
        ShcneUpdateItem = 0x00002000  # SHCNE_UPDATEITEM
        ShcnfPathW = 0x0005           # SHCNF_PATHW
        ShcnfFlushNoWait = 0x2000     # SHCNF_FLUSHNOWAIT
        MaxNotifyPath = 260           # MAX_PATH, including the terminating NUL
    }
}

function Assert-WorkStackShortcutInstallPath {
    <#
        .SYNOPSIS
        The interactive install-path policy, shared by Install and Apply.

        .DESCRIPTION
        The default interactive installer only writes under LOCALAPPDATA\Programs.
        The caller's ORIGINAL -NoShortcut switch is the intent that waives it;
        an internally forced suppression must never be able to bypass it, which
        is why callers pass their original switch here rather than the value
        they are about to hand the updater.

        Pure: compares normalized paths and throws. No filesystem access.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$InstallPath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$LocalProgramsPath,
        [bool]$OriginalNoShortcut = $false
    )

    if ($OriginalNoShortcut) { return }
    $normalizedInstall = [IO.Path]::GetFullPath($InstallPath)
    $normalizedPrograms = [IO.Path]::GetFullPath($LocalProgramsPath)
    $prefix = $normalizedPrograms + [IO.Path]::DirectorySeparatorChar
    if (-not $normalizedInstall.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The default interactive installer only writes under LOCALAPPDATA\Programs.'
    }
}

function Get-WorkStackShortcutIconPath {
    <#
        .SYNOPSIS
        The installed versioned icon leaf every managed link points at.

        .DESCRIPTION
        The admitted packaged asset is used as installed. It is never
        regenerated, edited, or copied to a new root icon: the old GDI generator
        and the root WorkStack.ico it produced are gone.
    #>
    param([Parameter(Mandatory = $true)][string]$InstallPath)

    return Join-Path $InstallPath 'desktop\python-webview-shell\assets\WorkStack-Mark-Lime-v2.ico'
}

function Assert-WorkStackShortcutIconAsset {
    <#
        .SYNOPSIS
        The packaged icon must exist as a leaf before any destructive effect.

        .DESCRIPTION
        Called by Install before it begins destructive installation effects, and
        by the finalizer before it saves any link, so a missing or non-leaf asset
        refuses early instead of producing links that point at nothing.
    #>
    param([Parameter(Mandatory = $true)][string]$IconPath)

    if (-not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
        throw "The packaged Work Stack icon is missing: $IconPath"
    }
}

function Get-WorkStackManagedShortcut {
    <#
        .SYNOPSIS
        The exact three managed links, as data.

        .DESCRIPTION
        Pure: builds descriptors only. Targets, arguments and working
        directories are unchanged from the previous inline implementation; only
        the icon moved to the installed versioned asset.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$InstallPath,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$StartMenuPath,
        [Parameter(Mandatory = $true)][string]$DesktopPath
    )

    $iconLocation = '{0},0' -f (Get-WorkStackShortcutIconPath -InstallPath $InstallPath)
    # The branded same-process host is the desktop launch target. The bundled
    # pythonw.exe stays in the payload for legacy installations but is no longer
    # the target of a newly written link.
    $launcher = Join-Path $InstallPath 'WorkStack.exe'
    $entry = Join-Path $InstallPath 'desktop\python-webview-shell\workstack_desktop.py'
    # Correct Windows argument encoding, so a valid absolute path ending in a
    # separator round-trips instead of escaping its own closing quote.
    $applicationArguments = @(
        (ConvertTo-WorkStackCommandLineArgument -Value $entry -AlwaysQuote),
        '--install-root',
        (ConvertTo-WorkStackCommandLineArgument -Value $InstallPath -AlwaysQuote),
        '--state-root',
        (ConvertTo-WorkStackCommandLineArgument -Value $StatePath -AlwaysQuote)
    ) -join ' '
    $maintenance = Join-Path $InstallPath 'scripts\windows\Maintain-WorkStack.ps1'
    # The maintenance link carries exactly its existing tokens, now through the same
    # encoder so a trailing state separator or a quote character survives Save.
    $maintenanceArguments = @(
        '-NoProfile',
        '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass',
        '-File', (ConvertTo-WorkStackCommandLineArgument -Value $maintenance -AlwaysQuote),
        '-InstallRoot', (ConvertTo-WorkStackCommandLineArgument -Value $InstallPath -AlwaysQuote),
        '-StateRoot', (ConvertTo-WorkStackCommandLineArgument -Value $StatePath -AlwaysQuote)
    ) -join ' '

    return @(
        [pscustomobject]@{
            Path = Join-Path $StartMenuPath 'Work Stack.lnk'
            TargetPath = $launcher
            Arguments = $applicationArguments
            WorkingDirectory = $InstallPath
            IconLocation = $iconLocation
        },
        [pscustomobject]@{
            Path = Join-Path $DesktopPath 'Work Stack.lnk'
            TargetPath = $launcher
            Arguments = $applicationArguments
            WorkingDirectory = $InstallPath
            IconLocation = $iconLocation
        },
        [pscustomobject]@{
            Path = Join-Path $StartMenuPath 'Work Stack Maintenance.lnk'
            TargetPath = 'powershell.exe'
            Arguments = $maintenanceArguments
            WorkingDirectory = $InstallPath
            IconLocation = $iconLocation
        }
    )
}

function New-WorkStackShellChangeNotifier {
    <#
        .SYNOPSIS
        Lazily create the bounded SHChangeNotify binding.

        .DESCRIPTION
        Add-Type runs here, never at load, so dot-sourcing stays inert. The
        declaration is Unicode with ExactSpelling and explicit LPWStr marshalling
        and a void return: the API cannot report whether Explorer refreshed, so
        no success is inferred from calling it.
    #>

    if (-not ('WorkStack.ShellChangeNotify' -as [type])) {
        Add-Type -Namespace 'WorkStack' -Name 'ShellChangeNotify' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, ExactSpelling = true)]
public static extern void SHChangeNotify(
    int wEventId,
    uint uFlags,
    [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.LPWStr)] string dwItem1,
    System.IntPtr dwItem2);
'@
    }
    return [WorkStack.ShellChangeNotify]
}

function Send-WorkStackShortcutNotification {
    <#
        .SYNOPSIS
        Announce one managed link path, or report it unsupported.

        .DESCRIPTION
        Only the exact managed paths are announced; nothing is enumerated. No
        cache deletion, pin editing, SHCNE_ASSOCCHANGED, Explorer restart or
        process launch happens here. A path beyond the API's MAX_PATH input limit
        is reported unsupported rather than silently treated as refreshed.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Existed,
        [scriptblock]$Notifier
    )

    $constants = Get-WorkStackShortcutNotificationConstant
    # MAX_PATH includes the terminating NUL, so a 260-character path does not
    # fit and must be refused with no announcement at all.
    if ($Path.Length -ge $constants.MaxNotifyPath) {
        return [pscustomobject]@{
            Path = $Path
            Notified = $false
            Reason = 'path exceeds the shell notification path limit'
        }
    }
    $event = if ($Existed) { $constants.ShcneUpdateItem } else { $constants.ShcneCreate }
    $flags = $constants.ShcnfPathW -bor $constants.ShcnfFlushNoWait
    if ($Notifier) {
        & $Notifier $event $flags $Path
    } else {
        $api = New-WorkStackShellChangeNotifier
        $api::SHChangeNotify($event, [uint32]$flags, $Path, [IntPtr]::Zero)
    }
    # The API returns void, so this records that the announcement was issued,
    # never that Explorer actually refreshed.
    return [pscustomobject]@{ Path = $Path; Notified = $true; Reason = '' }
}

function Invoke-WorkStackShortcutFinalization {
    <#
        .SYNOPSIS
        Create or refresh the three managed links, then announce them.

        .DESCRIPTION
        Derived, post-commit work. Saves first, then notifies only the paths that
        were saved. Returns a result object; it does not decide policy. The
        caller turns a failure into an applied-with-warning receipt and exit 0.

        Every external effect is injectable so tests can substitute COM, the
        shell folders and the native notification without touching the real
        Shell.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$InstallPath,
        [Parameter(Mandatory = $true)][string]$StatePath,
        [string]$StartMenuPath,
        [string]$DesktopPath,
        [scriptblock]$ShortcutFactory,
        [scriptblock]$Notifier,
        [scriptblock]$ExistenceProbe
    )

    if (-not $StartMenuPath) {
        $StartMenuPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    }
    if (-not $DesktopPath) {
        $DesktopPath = [Environment]::GetFolderPath('Desktop')
    }

    $iconPath = Get-WorkStackShortcutIconPath -InstallPath $InstallPath
    Assert-WorkStackShortcutIconAsset -IconPath $iconPath

    $descriptors = Get-WorkStackManagedShortcut -InstallPath $InstallPath -StatePath $StatePath -StartMenuPath $StartMenuPath -DesktopPath $DesktopPath
    $saved = @()
    $notifications = @()

    foreach ($descriptor in $descriptors) {
        # Existence is captured for these exact paths only, before the save, so
        # the announcement can distinguish a created link from an updated one.
        $existed = if ($ExistenceProbe) {
            [bool](& $ExistenceProbe $descriptor.Path)
        } else {
            Test-Path -LiteralPath $descriptor.Path -PathType Leaf
        }
        $shortcut = if ($ShortcutFactory) {
            & $ShortcutFactory $descriptor.Path
        } else {
            (New-Object -ComObject WScript.Shell).CreateShortcut($descriptor.Path)
        }
        $shortcut.TargetPath = $descriptor.TargetPath
        $shortcut.Arguments = $descriptor.Arguments
        $shortcut.WorkingDirectory = $descriptor.WorkingDirectory
        # Set before Save, so the versioned icon is part of the saved link.
        $shortcut.IconLocation = $descriptor.IconLocation
        $shortcut.Save()
        $saved += [pscustomobject]@{ Path = $descriptor.Path; Existed = $existed }
    }

    foreach ($entry in $saved) {
        $notifications += Send-WorkStackShortcutNotification -Path $entry.Path -Existed $entry.Existed -Notifier $Notifier
    }

    $unsupported = @($notifications | Where-Object { -not $_.Notified })
    return [pscustomobject]@{
        Saved = @($saved | ForEach-Object { $_.Path })
        Notifications = $notifications
        Complete = ($unsupported.Count -eq 0)
        IncompleteReason = if ($unsupported.Count -gt 0) {
            'shell notification unsupported for {0} managed link(s)' -f $unsupported.Count
        } else { '' }
    }
}
