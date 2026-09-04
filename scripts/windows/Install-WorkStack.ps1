[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [string]$DataDir = "$env:LOCALAPPDATA\WorkStack\data",
    [string]$BackupDir = '',
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
$configPath = Join-Path $statePath 'config.json'
$originalConfigBytes = $null
$originalConfigAttributes = $null
$existingConfig = $null
$configWasMutated = $false
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    $originalConfigBytes = [IO.File]::ReadAllBytes($configPath)
    $originalConfigAttributes = [IO.File]::GetAttributes($configPath)
    try {
        $existingConfig = [Text.Encoding]::UTF8.GetString($originalConfigBytes) | ConvertFrom-Json
    } catch {
        throw "Existing Work Stack configuration is invalid: $configPath"
    }
    if (-not $PSBoundParameters.ContainsKey('DataDir')) {
        $existingDataDir = [string]$existingConfig.data_dir
        if ([string]::IsNullOrWhiteSpace($existingDataDir) -or -not [IO.Path]::IsPathRooted($existingDataDir)) {
            throw "Existing Work Stack data directory is invalid: $configPath"
        }
        $dataPath = [IO.Path]::GetFullPath($existingDataDir)
    }
    if (-not $PSBoundParameters.ContainsKey('Port') -and $null -ne $existingConfig.port) {
        $Port = [int]$existingConfig.port
    }
    if (-not $PSBoundParameters.ContainsKey('BackupRetention') -and $null -ne $existingConfig.backup_retention) {
        $BackupRetention = [int]$existingConfig.backup_retention
    }
    if ((-not $PSBoundParameters.ContainsKey('BackupDir') -or [string]::IsNullOrWhiteSpace($BackupDir)) -and
        -not [string]::IsNullOrWhiteSpace([string]$existingConfig.backup_dir)) {
        $BackupDir = [string]$existingConfig.backup_dir
    }
}
if ([string]::IsNullOrWhiteSpace($BackupDir)) { $BackupDir = Join-Path $statePath 'backups' }
if (-not [IO.Path]::IsPathRooted($BackupDir)) { throw 'BackupDir must be an absolute path.' }
$backupRoot = [IO.Path]::GetFullPath($BackupDir)

function Get-InstallerAuthority {
    param([Parameter(Mandatory = $true)][string]$RuntimeRoot)
    $authorityPython = Join-Path $RuntimeRoot 'runtime\python.exe'
    $authorityResolver = Join-Path $RuntimeRoot 'scripts\windows\Resolve-WorkStackInstallerAuthority.py'
    if (-not (Test-Path -LiteralPath $authorityPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath $authorityResolver -PathType Leaf)) {
        throw 'Bundled installer authority reader is unavailable.'
    }
    $authorityOutput = & $authorityPython -B $authorityResolver --state-root $statePath 2>$null
    $authorityExit = $LASTEXITCODE
    try { $authority = ($authorityOutput -join "`n") | ConvertFrom-Json }
    catch { throw 'Bundled installer authority response is invalid.' }
    if ($authorityExit -ne 0) {
        $code = [string]$authority.code
        if ($code -notmatch '^[a-z_]{1,64}$') { $code = 'unavailable' }
        throw "Local installer authority refused ($code)."
    }
    if ($authority.status -notin @('selected', 'absent-registry') -or
        [string]$authority.binding -cnotmatch '^sha256:[0-9a-f]{64}$') {
        throw 'Bundled installer authority response is invalid.'
    }
    return $authority
}

$initialAuthority = Get-InstallerAuthority -RuntimeRoot $sourcePath
if ($initialAuthority.status -eq 'selected') {
    $selectedDataPath = [IO.Path]::GetFullPath([string]$initialAuthority.data_dir)
    if ($PSBoundParameters.ContainsKey('DataDir') -and
        -not $dataPath.Equals($selectedDataPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Explicit DataDir conflicts with the selected local connection profile.'
    }
    $dataPath = $selectedDataPath
    Write-Host "Using selected local connection profile $($initialAuthority.profile_id)."
}

function Write-BytesAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes
    )
    $target = [IO.Path]::GetFullPath($Path)
    $temporary = "$target.tmp-$PID-$([guid]::NewGuid().ToString('N'))"
    try {
        [IO.File]::WriteAllBytes($temporary, $Bytes)
        Move-Item -LiteralPath $temporary -Destination $target -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Remove-DirectoryWithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [int]$Attempts = 20,
        [int]$DelayMilliseconds = 250
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            if (-not (Test-Path -LiteralPath $Path)) { return $true }
            Remove-Item -LiteralPath $Path -Recurse -Force
            return $true
        } catch {
            if ($attempt -eq $Attempts) { return $false }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
    return $false
}

function Write-Utf8NoBomAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    Write-BytesAtomic -Path $Path -Bytes ([Text.UTF8Encoding]::new($false).GetBytes($Value))
}

function Restore-OriginalConfig {
    if ($null -ne $originalConfigBytes) {
        if (Test-Path -LiteralPath $configPath) {
            [IO.File]::SetAttributes($configPath, [IO.FileAttributes]::Normal)
        }
        Write-BytesAtomic -Path $configPath -Bytes $originalConfigBytes
        if ($null -ne $originalConfigAttributes) {
            [IO.File]::SetAttributes($configPath, $originalConfigAttributes)
        }
    } elseif ($configWasMutated -and (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        Remove-Item -LiteralPath $configPath -Force
    }
}

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

function Test-LoopbackPortAvailable {
    param([Parameter(Mandatory = $true)][int]$CandidatePort)

    $listener = $null
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $CandidatePort)
        $listener.Start()
        return $true
    } catch [Net.Sockets.SocketException] {
        return $false
    } finally {
        if ($null -ne $listener) { $listener.Stop() }
    }
}

function Resolve-AvailableLoopbackPort {
    param([Parameter(Mandatory = $true)][int]$PreferredPort)

    $lastCandidate = [Math]::Min(65535, $PreferredPort + 100)
    for ($candidate = $PreferredPort; $candidate -le $lastCandidate; $candidate++) {
        if (Test-LoopbackPortAvailable -CandidatePort $candidate) { return $candidate }
    }
    throw "No available loopback port was found between $PreferredPort and $lastCandidate."
}

Assert-PathsDisjoint -First $installPath -Second $statePath -Description 'install/state'
Assert-PathsDisjoint -First $installPath -Second $dataPath -Description 'install/data'
Assert-PathsDisjoint -First $installPath -Second $backupRoot -Description 'install/backup'
Assert-PathsDisjoint -First $dataPath -Second $backupRoot -Description 'data/backup'

. (Join-Path $PSScriptRoot 'WorkStack-Shortcuts.ps1')

$localPrograms = [IO.Path]::GetFullPath("$env:LOCALAPPDATA\Programs")
Assert-WorkStackShortcutInstallPath -InstallPath $installPath -LocalProgramsPath $localPrograms -OriginalNoShortcut ([bool]$NoShortcut)
foreach ($required in @('workstack', 'frontend\dist', 'run_work_stack.py', 'requirements.txt', 'requirements-windows-desktop.txt', 'scripts\windows', 'desktop\python-webview-shell\workstack_desktop.py', 'runtime\python.exe', 'runtime\pythonw.exe', 'runtime\python312.dll', 'WorkStack.exe')) {
    $requiredPath = Join-Path $sourcePath $required
    if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Installer source is missing $required" }
    if ($required -in @('run_work_stack.py', 'desktop\python-webview-shell\workstack_desktop.py', 'runtime\python.exe', 'runtime\pythonw.exe', 'runtime\python312.dll', 'WorkStack.exe') -and
        -not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        # A directory carrying a critical file's name is not that file.
        throw "Installer source is not a file: $required"
    }
}
# The packaged icon must be a real leaf in the source before any destructive
# effect, so a missing asset refuses here rather than after the payload moves.
Assert-WorkStackShortcutIconAsset -IconPath (Get-WorkStackShortcutIconPath -InstallPath $sourcePath)
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
    foreach ($file in @('run_work_stack.py', 'requirements.txt', 'requirements-windows-desktop.txt', 'README.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md', 'WorkStack.exe')) {
        # The installation-root host is staged by this loop; without it the staged
        # guard below can never be satisfied by a genuine payload.
        Copy-Item -LiteralPath (Join-Path $sourcePath $file) -Destination (Join-Path $staging $file)
    }

    $stagedPython = Join-Path $staging 'runtime\python.exe'
    $pythonTarget = & $stagedPython -c "import jsonschema,struct,sys,unicodedata2,workstack,webview; from webview.platforms.edgechromium import WebView2; assert unicodedata2.unidata_version == '17.0.0'; print(f'{sys.version_info.major}.{sys.version_info.minor}:{struct.calcsize(chr(80))*8}')" 2>$null
    if ($LASTEXITCODE -ne 0 -or $pythonTarget.Trim() -ne '3.12:64') {
        throw 'Bundled 64-bit Python 3.12 runtime smoke test failed.'
    }

    # Revalidate the complete authority binding with staged code before effects.
    $revalidatedAuthority = Get-InstallerAuthority -RuntimeRoot $staging
    if ($revalidatedAuthority.binding -cne $initialAuthority.binding) {
        throw 'Local installer authority changed during staging; installation was not changed.'
    }

    # The source was validated before staging, but the source can lose the
    # asset between that check and the recursive copy. Validate the STAGED
    # leaf as well, after the copy and before Stop, the pre-upgrade backup and
    # the payload moves, so an incomplete stage refuses before any destructive
    # effect. The transactional install's forced -NoShortcut suppresses links,
    # not asset integrity, so this guard runs regardless of that switch.
    Assert-WorkStackShortcutIconAsset -IconPath (Get-WorkStackShortcutIconPath -InstallPath $staging)
    foreach ($stagedLeaf in @('WorkStack.exe', 'desktop\python-webview-shell\workstack_desktop.py', 'runtime\python312.dll')) {
        # Revalidated on the staged tree, before Stop-WorkStack, the pre-upgrade
        # backup or any payload move can change the installation.
        if (-not (Test-Path -LiteralPath (Join-Path $staging $stagedLeaf) -PathType Leaf)) {
            throw "The staged Work Stack desktop host is incomplete: $stagedLeaf"
        }
    }

    if (Test-Path -LiteralPath $installPath) {
        $stopScript = Join-Path $staging 'scripts\windows\Stop-WorkStack.ps1'
        if (Test-Path -LiteralPath $stopScript) { & $stopScript -InstallRoot $installPath }
        $stagedEntry = Join-Path $staging 'run_work_stack.py'
        if (Test-Path -LiteralPath (Join-Path $dataPath 'workspace.json')) {
            # The installed runtime may be incomplete or damaged, which is one of
            # the conditions an upgrade must be able to repair.  Use the already
            # smoke-tested staged runtime to create the read-only safety backup.
            & $stagedPython $stagedEntry --data-dir $dataPath maintenance backup --out $backupRoot | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Pre-upgrade backup failed; installation was not changed.' }
        }
        Move-Item -LiteralPath $installPath -Destination $rollback
    }
    $resolvedPort = Resolve-AvailableLoopbackPort -PreferredPort $Port
    if ($resolvedPort -ne $Port) {
        Write-Warning "Port $Port is already in use; Work Stack will use $resolvedPort instead."
    }
    Move-Item -LiteralPath $staging -Destination $installPath

    New-Item -ItemType Directory -Force -Path $statePath, $dataPath, $backupRoot | Out-Null
    $configValues = [ordered]@{}
    if ($null -ne $existingConfig) {
        foreach ($property in $existingConfig.PSObject.Properties) {
            $configValues[$property.Name] = $property.Value
        }
    }
    $configValues['version'] = 1
    $configValues['install_dir'] = $installPath
    $configValues['data_dir'] = $dataPath
    $configValues['backup_dir'] = $backupRoot
    $configValues['backup_retention'] = [Math]::Max(1, $BackupRetention)
    $configValues['port'] = $resolvedPort
    $configJson = $configValues | ConvertTo-Json
    $preserveExistingConfig = $false
    if ($null -ne $existingConfig) {
        $preserveExistingConfig =
            ([IO.Path]::GetFullPath([string]$existingConfig.install_dir) -eq $installPath) -and
            ([IO.Path]::GetFullPath([string]$existingConfig.data_dir) -eq $dataPath) -and
            ([IO.Path]::GetFullPath([string]$existingConfig.backup_dir) -eq $backupRoot) -and
            ([int]$existingConfig.backup_retention -eq [Math]::Max(1, $BackupRetention)) -and
            ([int]$existingConfig.port -eq $resolvedPort)
    }
    if ($preserveExistingConfig) {
        $configBytes = $originalConfigBytes
    } else {
        Write-Utf8NoBomAtomic -Path $configPath -Value $configJson
        $configWasMutated = $true
        $configBytes = [Text.UTF8Encoding]::new($false).GetBytes($configJson)
    }
    Write-BytesAtomic -Path (Join-Path $installPath 'runtime-config.json') -Bytes $configBytes
    if ([Environment]::GetEnvironmentVariable('WORKSTACK_INSTALL_TEST_FAIL_AFTER_CONFIG_WRITE', 'Process') -ceq '1') {
        throw 'Injected upgrade failure after configuration write.'
    }

    if (-not $NoShortcut) {
        # Standalone install finalizes at its ordinary shortcut stage, through
        # the same helper the update path uses after its commit boundary.
        Invoke-WorkStackShortcutFinalization -InstallPath $installPath -StatePath $statePath | Out-Null
    }
    if (-not (Remove-DirectoryWithRetry -Path $rollback)) {
        Write-Warning "Work Stack was installed, but the previous runtime is still locked and could not be removed: $rollback. It is safe to remove this rollback directory after Work Stack processes have exited."
    }
    Write-Host "Work Stack installed at $installPath"
    Write-Host "Planning data remains at $dataPath"
    Write-Host "Local endpoint: http://127.0.0.1:$resolvedPort/"
} catch {
    $installError = $_
    if (Test-Path -LiteralPath $rollback) {
        if (Test-Path -LiteralPath $installPath) {
            Remove-Item -LiteralPath $installPath -Recurse -Force
        }
        Move-Item -LiteralPath $rollback -Destination $installPath
    }
    try {
        Restore-OriginalConfig
    } catch {
        throw "Installation failed and the original configuration could not be restored: $($_.Exception.Message)"
    }
    throw $installError
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
