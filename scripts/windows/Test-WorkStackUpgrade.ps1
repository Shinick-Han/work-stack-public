[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PreviousSetupPath,
    [Parameter(Mandatory = $true)][string]$CandidateSetupPath,
    [string]$PreviousChecksumPath = '',
    [string]$CandidateChecksumPath = '',
    [string]$PreviousVersion = '1.0.5'
)

$ErrorActionPreference = 'Stop'
$previous = [IO.Path]::GetFullPath($PreviousSetupPath)
$candidate = [IO.Path]::GetFullPath($CandidateSetupPath)
$versionPattern = '\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\z'
if ($PreviousVersion -notmatch $versionPattern) { throw 'PreviousVersion must be canonical X.Y.Z.' }
if ([IO.Path]::GetFileName($previous) -cne "WorkStack-Setup-$PreviousVersion.ps1") {
    throw "PreviousSetupPath must be the exact Work Stack $PreviousVersion installer."
}
$candidateMatch = [regex]::Match([IO.Path]::GetFileName($candidate), '\AWorkStack-Setup-(?<version>\d+\.\d+\.\d+)\.ps1\z')
if (-not $candidateMatch.Success -or $candidateMatch.Groups['version'].Value -notmatch $versionPattern -or
    ([version]$candidateMatch.Groups['version'].Value -le [version]$PreviousVersion)) {
    throw 'CandidateSetupPath must identify a later canonical Work Stack installer.'
}
if (-not $PreviousChecksumPath) { $PreviousChecksumPath = "$previous.sha256" }
if (-not $CandidateChecksumPath) { $CandidateChecksumPath = "$candidate.sha256" }

$verifier = Join-Path $PSScriptRoot 'Test-WorkStackSetup.ps1'
& $verifier -SetupPath $previous -ChecksumPath $PreviousChecksumPath | Out-Null
& $verifier -SetupPath $candidate -ChecksumPath $CandidateChecksumPath | Out-Null

function Set-IsReadOnly {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][bool]$Value)
    $item = Get-Item -LiteralPath $Path
    $item.IsReadOnly = $Value
}

function Get-InstalledVersion {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $source = [IO.File]::ReadAllText((Join-Path $InstallRoot 'workstack\__init__.py'))
    $match = [regex]::Match($source, '(?m)^__version__\s*=\s*"(?<version>[^"]+)"')
    if (-not $match.Success) { throw 'Installed payload has no version.' }
    return $match.Groups['version'].Value
}

function Initialize-TestSsot {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$DataDir
    )
    $python = Join-Path $InstallRoot 'runtime\python.exe'
    $previousRuntime = [Environment]::GetEnvironmentVariable('WORK_STACK_RUNTIME', 'Process')
    try {
        [Environment]::SetEnvironmentVariable(
            'WORK_STACK_RUNTIME', (Join-Path $StateRoot 'runtime'), 'Process'
        )
        & $python -c "import sys; from pathlib import Path; from workstack.service import WorkStack; from workstack.store import Store; stack=WorkStack(Store(Path(sys.argv[1]))); task=stack.add_task('Release gate Task', detail='byte-exact SSOT canary'); stack.add_task_note(task['id'], 'preserve every authoritative byte')" $DataDir
        if ($LASTEXITCODE -ne 0) { throw 'The previous installer could not initialize the SSOT canary.' }
    } finally {
        [Environment]::SetEnvironmentVariable('WORK_STACK_RUNTIME', $previousRuntime, 'Process')
    }
}

function Get-SsotByteManifest {
    param([Parameter(Mandatory = $true)][string]$DataDir)
    $root = [IO.Path]::GetFullPath($DataDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $entries = foreach ($file in Get-ChildItem -LiteralPath $root -File -Recurse -Force | Sort-Object FullName) {
        $relative = $file.FullName.Substring($root.Length).TrimStart([IO.Path]::DirectorySeparatorChar)
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
        "$relative|$($file.Length)|$digest"
    }
    if (@($entries).Count -lt 10) { throw 'The SSOT canary did not contain the released document roster.' }
    return @($entries) -join "`n"
}

function Assert-SsotByteManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$DataDir,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    if ((Get-SsotByteManifest -DataDir $DataDir) -cne $Expected) { throw $FailureMessage }
}

function Write-TestConfig {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BackupDir
    )
    $config = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    $ordered = [ordered]@{
        version = [int]$config.version
        install_dir = [string]$config.install_dir
        data_dir = [string]$config.data_dir
        backup_dir = [IO.Path]::GetFullPath($BackupDir)
        backup_retention = 37
        port = [int]$config.port
        future_setting = 'preserve-exactly'
    }
    $body = $ordered | ConvertTo-Json
    [IO.File]::WriteAllText($Path, $body, [Text.UTF8Encoding]::new($false))
}

$root = Join-Path ([IO.Path]::GetTempPath()) ("workstack-upgrade-smoke-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
try {
    $preserveInstall = Join-Path $root 'preserve-install'
    $preserveState = Join-Path $root 'preserve-state'
    $preserveData = Join-Path $preserveState 'data'
    $preserveBackup = Join-Path $root 'custom-backups\preserve'
    & $previous -InstallRoot $preserveInstall -StateRoot $preserveState -DataDir $preserveData -Port 19165 -NoShortcut
    Initialize-TestSsot -InstallRoot $preserveInstall -StateRoot $preserveState -DataDir $preserveData
    $preserveConfig = Join-Path $preserveState 'config.json'
    Write-TestConfig -Path $preserveConfig -BackupDir $preserveBackup
    $originalConfigBytes = [IO.File]::ReadAllBytes($preserveConfig)
    $marker = Join-Path $preserveData 'release-gate-marker.txt'
    [IO.File]::WriteAllText($marker, 'preserve-me', [Text.UTF8Encoding]::new($false))
    $preserveSsotManifest = Get-SsotByteManifest -DataDir $preserveData
    New-Item -ItemType Directory -Force -Path $preserveBackup | Out-Null
    $preserveBackupMarker = Join-Path $preserveBackup 'custom-backup-marker.txt'
    [IO.File]::WriteAllText($preserveBackupMarker, 'preserve-custom-backup', [Text.UTF8Encoding]::new($false))
    Set-IsReadOnly -Path $preserveConfig -Value $true
    try {
        & (Join-Path $PSScriptRoot 'Update-WorkStack.ps1') -SetupPath $candidate -ChecksumPath $CandidateChecksumPath -InstallRoot $preserveInstall -StateRoot $preserveState -NoShortcut
    } finally {
        Set-IsReadOnly -Path $preserveConfig -Value $false
    }
    if (-not [Linq.Enumerable]::SequenceEqual([byte[]]$originalConfigBytes, [byte[]][IO.File]::ReadAllBytes($preserveConfig))) {
        throw "Configuration bytes were not preserved across the $PreviousVersion upgrade."
    }
    if ([IO.File]::ReadAllText($marker) -cne 'preserve-me') { throw 'The SSOT marker was not preserved.' }
    Assert-SsotByteManifest -Expected $preserveSsotManifest -DataDir $preserveData `
        -FailureMessage "Authoritative SSOT bytes were not preserved across the $PreviousVersion upgrade."
    if ([IO.File]::ReadAllText($preserveBackupMarker) -cne 'preserve-custom-backup') {
        throw 'The custom backup directory was not preserved across the successful upgrade.'
    }
    if ((Get-InstalledVersion -InstallRoot $preserveInstall) -cne $candidateMatch.Groups['version'].Value) {
        throw 'The candidate payload was not installed.'
    }

    $rollbackInstall = Join-Path $root 'rollback-install'
    $rollbackState = Join-Path $root 'rollback-state'
    $rollbackData = Join-Path $rollbackState 'data'
    $rollbackBackup = Join-Path $root 'custom-backups\rollback'
    & $previous -InstallRoot $rollbackInstall -StateRoot $rollbackState -DataDir $rollbackData -Port 19265 -NoShortcut
    Initialize-TestSsot -InstallRoot $rollbackInstall -StateRoot $rollbackState -DataDir $rollbackData
    $rollbackConfig = Join-Path $rollbackState 'config.json'
    Write-TestConfig -Path $rollbackConfig -BackupDir $rollbackBackup
    $rollbackConfigBytes = [IO.File]::ReadAllBytes($rollbackConfig)
    $rollbackMarker = Join-Path $rollbackData 'release-gate-marker.txt'
    [IO.File]::WriteAllText($rollbackMarker, 'rollback-preserve-me', [Text.UTF8Encoding]::new($false))
    $rollbackSsotManifest = Get-SsotByteManifest -DataDir $rollbackData
    New-Item -ItemType Directory -Force -Path $rollbackBackup | Out-Null
    $rollbackBackupMarker = Join-Path $rollbackBackup 'custom-backup-marker.txt'
    [IO.File]::WriteAllText($rollbackBackupMarker, 'rollback-custom-backup', [Text.UTF8Encoding]::new($false))
    $failed = $false
    $previousFailpoint = [Environment]::GetEnvironmentVariable('WORKSTACK_INSTALL_TEST_FAIL_AFTER_CONFIG_WRITE', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('WORKSTACK_INSTALL_TEST_FAIL_AFTER_CONFIG_WRITE', '1', 'Process')
        & $candidate -InstallRoot $rollbackInstall -StateRoot $rollbackState -DataDir $rollbackData -BackupDir $rollbackBackup -Port 19266 -BackupRetention 37 -NoShortcut
    } catch {
        $failed = $true
    } finally {
        [Environment]::SetEnvironmentVariable('WORKSTACK_INSTALL_TEST_FAIL_AFTER_CONFIG_WRITE', $previousFailpoint, 'Process')
    }
    if (-not $failed) { throw 'The rollback smoke did not trigger the intended post-swap failure.' }
    if ((Get-InstalledVersion -InstallRoot $rollbackInstall) -cne $PreviousVersion) {
        throw "Rollback did not restore the $PreviousVersion payload."
    }
    if (-not [Linq.Enumerable]::SequenceEqual([byte[]]$rollbackConfigBytes, [byte[]][IO.File]::ReadAllBytes($rollbackConfig))) {
        throw 'Rollback did not preserve the original configuration bytes.'
    }
    if ([IO.File]::ReadAllText($rollbackMarker) -cne 'rollback-preserve-me') {
        throw 'Rollback did not preserve the SSOT marker.'
    }
    Assert-SsotByteManifest -Expected $rollbackSsotManifest -DataDir $rollbackData `
        -FailureMessage 'Rollback did not preserve every authoritative SSOT byte.'
    if ([IO.File]::ReadAllText($rollbackBackupMarker) -cne 'rollback-custom-backup') {
        throw 'Rollback did not preserve the custom backup directory.'
    }

    $applyInstall = Join-Path $root 'apply-install'
    $applyState = Join-Path $root 'apply-state'
    $applyData = Join-Path $applyState 'data'
    $applyBackup = Join-Path $root 'custom-backups\apply'
    & $previous -InstallRoot $applyInstall -StateRoot $applyState -DataDir $applyData -Port 19365 -NoShortcut
    Initialize-TestSsot -InstallRoot $applyInstall -StateRoot $applyState -DataDir $applyData
    $applyConfig = Join-Path $applyState 'config.json'
    Write-TestConfig -Path $applyConfig -BackupDir $applyBackup
    $applyConfigBytes = [IO.File]::ReadAllBytes($applyConfig)
    $applyMarker = Join-Path $applyData 'release-gate-marker.txt'
    [IO.File]::WriteAllText($applyMarker, 'apply-rollback-preserve-me', [Text.UTF8Encoding]::new($false))
    $applySsotManifest = Get-SsotByteManifest -DataDir $applyData
    $previousApplyFailpoint = [Environment]::GetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_RESTART', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_RESTART', '1', 'Process')
        $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
        & $powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Apply-WorkStackUpdate.ps1') `
            -SetupPath $candidate -ChecksumPath $CandidateChecksumPath -InstallRoot $applyInstall `
            -StateRoot $applyState -ParentProcessId 2147483647 -TargetVersion $candidateMatch.Groups['version'].Value -NoShortcut
        $applyExitCode = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('WORKSTACK_UPDATE_TEST_FAIL_RESTART', $previousApplyFailpoint, 'Process')
    }
    if ($applyExitCode -eq 0) { throw 'Post-install launcher rollback smoke unexpectedly succeeded.' }
    if ((Get-InstalledVersion -InstallRoot $applyInstall) -cne $PreviousVersion) {
        throw "Post-install launcher rollback did not restore the $PreviousVersion payload."
    }
    if (-not [Linq.Enumerable]::SequenceEqual([byte[]]$applyConfigBytes, [byte[]][IO.File]::ReadAllBytes($applyConfig))) {
        throw 'Post-install launcher rollback did not preserve the original configuration bytes.'
    }
    if ([IO.File]::ReadAllText($applyMarker) -cne 'apply-rollback-preserve-me') {
        throw 'Post-install launcher rollback did not preserve the SSOT marker.'
    }
    Assert-SsotByteManifest -Expected $applySsotManifest -DataDir $applyData `
        -FailureMessage 'Post-install launcher rollback did not preserve every authoritative SSOT byte.'
    $applyReceipt = Get-Content -Raw -LiteralPath (Join-Path $applyState 'updates\last-update.json') | ConvertFrom-Json
    if ([string]$applyReceipt.status -cne 'rolled-back' -or $null -ne $applyReceipt.recovery_path) {
        throw "Post-install launcher rollback did not record a terminal rolled-back receipt (status=$($applyReceipt.status), recovery_path=$($applyReceipt.recovery_path), message=$($applyReceipt.message))."
    }
    Write-Output "VERIFIED UPGRADE $PreviousVersion -> $($candidateMatch.Groups['version'].Value) WITH CUSTOM BACKUP/CONFIG/SSOT PRESERVATION, INSTALLER ROLLBACK, AND POST-INSTALL LAUNCHER ROLLBACK"
} finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
