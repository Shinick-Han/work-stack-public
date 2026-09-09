[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$OutputPath = '',
    [string]$RuntimeArchivePath = '',
    # Optional, and paired. Neither path builds the Windows product exactly as
    # it built before: local-only, with no remote payload. Both paths ship the
    # already built Linux remote artifact that the desktop update flow sends
    # over SSH. Exactly one is a build error, not a half-shipped installer.
    [string]$LinuxArtifactArchivePath = '',
    [string]$LinuxArtifactSidecarPath = '',
    # Consume the frontend\dist an earlier step of this same release already
    # admitted, instead of producing one here. Off by default: a standalone
    # build still refreshes its own dist, exactly as it always did. On, the
    # builder asks the same shared release gate to VERIFY the recorded
    # source/dist binding rather than to refresh it, so the release performs
    # one real frontend build and this packaging step performs none. Nothing
    # is skipped and no evidence is regenerated: an absent, stale or tampered
    # receipt or dist refuses here exactly as a failed refresh would.
    [switch]$ConsumeAdmittedDist,
    [switch]$SkipWheelDownload
)

$ErrorActionPreference = 'Stop'
$script:WorkStackPreserveTemporary = $false
. (Join-Path $PSScriptRoot 'WorkStack-Shortcuts.ps1')
$runtimeUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
$runtimeFilename = 'python-3.12.10-embed-amd64.zip'
$runtimeSha256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'

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

function Remove-PythonBytecode {
    param([Parameter(Mandatory = $true)][string]$Root)

    $separator = [IO.Path]::DirectorySeparatorChar
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd($separator)
    $cacheDirectories = Get-ChildItem -LiteralPath $resolvedRoot -Directory -Filter '__pycache__' -Recurse -Force |
        Sort-Object FullName -Descending
    foreach ($cacheDirectory in $cacheDirectories) {
        $resolved = [IO.Path]::GetFullPath($cacheDirectory.FullName)
        if (-not $resolved.StartsWith($resolvedRoot + $separator, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove bytecode cache outside the payload: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    $bytecodeFiles = Get-ChildItem -LiteralPath $resolvedRoot -File -Recurse -Force |
        Where-Object { $_.Extension -in @('.pyc', '.pyo') }
    foreach ($bytecodeFile in $bytecodeFiles) {
        $resolved = [IO.Path]::GetFullPath($bytecodeFile.FullName)
        if (-not $resolved.StartsWith($resolvedRoot + $separator, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove bytecode outside the payload: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Force
    }
}

if (-not $SourceRoot) { $SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
$sourcePath = [IO.Path]::GetFullPath($SourceRoot)
$versionLine = Get-Content -LiteralPath (Join-Path $sourcePath 'workstack\__init__.py') |
    Where-Object { $_ -match '^__version__\s*=\s*"([^"]+)"' } |
    Select-Object -First 1
if (-not $versionLine) { throw 'Work Stack version could not be read.' }
$version = [regex]::Match($versionLine, '"([^"]+)"').Groups[1].Value
# ---- paired Linux remote artifact selection --------------------------
# Decided before anything is created or downloaded, so a half-supplied
# selection costs nothing and leaves nothing behind.
$includesRemotePayload = [bool]$LinuxArtifactArchivePath -and [bool]$LinuxArtifactSidecarPath
if (-not $includesRemotePayload -and ($LinuxArtifactArchivePath -or $LinuxArtifactSidecarPath)) {
    throw ('-LinuxArtifactArchivePath and -LinuxArtifactSidecarPath are one selection and must be ' +
        'supplied together. Supply both to ship the Linux remote payload, or neither to build a ' +
        'local-only Windows installer.')
}
# ---- end paired Linux remote artifact selection ----------------------
if (-not $OutputPath) { $OutputPath = Join-Path $sourcePath ".artifacts\WorkStack-Setup-$version.ps1" }
$output = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $output) | Out-Null

$temporary = Join-Path ([IO.Path]::GetTempPath()) ("workstack-bundle-" + [guid]::NewGuid().ToString('N'))
$payload = Join-Path $temporary 'payload'
$archive = Join-Path $temporary 'payload.zip'
New-Item -ItemType Directory -Path $payload | Out-Null
try {
    foreach ($directory in @('workstack', 'contracts', 'licenses', 'web', 'desktop')) {
        Copy-Item -LiteralPath (Join-Path $sourcePath $directory) -Destination (Join-Path $payload $directory) -Recurse
    }
    if (-not (Test-Path -LiteralPath (Join-Path $payload 'workstack\__init__.py') -PathType Leaf)) {
        throw 'Bundled Work Stack package was not copied into the payload root.'
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $payload 'frontend'), (Join-Path $payload 'scripts') | Out-Null
    # ---- admitted frontend dist -----------------------------------------
    # Bind frontend\dist to the source content it was built from before a single
    # byte of it is copied. One bounded invocation of the shared release gate: it
    # captures the frontend build inputs into a private scratch tree, runs the
    # real `npm run build` out of that capture, and records the captured bytes
    # beside the emitted tree. An already matching tree is left byte-for-byte
    # untouched. mtimes and the git commit are not evidence here; only content
    # digests are. The gate never installs dependencies -- an uninstalled
    # frontend\node_modules refuses instead of packaging a stale bundle.
    #
    # -ConsumeAdmittedDist selects the gate's `verify-dist` instead. That is
    # the same gate, the same receipt and the same admitted-tree answer -- it
    # re-reads the recorded build inputs and the emitted tree and refuses on
    # any drift -- but it never runs a build, so a release that already built
    # and froze the dist does not build a second one here. There is no third
    # mode and no bypass: exactly one of the two gate operations always runs,
    # and it must succeed and report an admitted digest before a dist byte is
    # copied. Consuming an already admitted tree is the only thing the switch
    # can do; it cannot make packaging accept an unadmitted one.
    $distGateCommand = if ($ConsumeAdmittedDist) { 'verify-dist' } else { 'refresh-dist' }
    $distGate = & python (Join-Path $sourcePath 'scripts\release_gate.py') $distGateCommand --repo $sourcePath 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "The frontend dist/source gate refused to package this tree ($distGateCommand): $($distGate -join ' ')"
    }
    # Keep what the gate ADMITTED. frontend\dist is generated and git-ignored, so
    # any other process on this machine may rewrite it the instant this call
    # returns; the admitted digest is the only thing that can bind the bytes this
    # installer ships to the receipt the gate just wrote.
    $distSummary = @($distGate | ForEach-Object { $_.ToString() } | Where-Object { $_.Trim() }) |
        Select-Object -Last 1
    $admittedDist = $null
    try { $admittedDist = $distSummary | ConvertFrom-Json } catch { $admittedDist = $null }
    if (-not $admittedDist -or -not $admittedDist.dist_digest) {
        throw "The frontend dist/source gate reported no admitted dist digest: $($distGate -join ' ')"
    }
    Write-Host "Frontend dist gate ($distGateCommand) admitted $($admittedDist.dist_file_count) file(s): $($admittedDist.dist_digest)"
    Copy-Item -LiteralPath (Join-Path $sourcePath 'frontend\dist') -Destination (Join-Path $payload 'frontend\dist') -Recurse
    # Everything shipped from here on is this private payload copy, and it is now
    # compared to the admitted digest rather than to a fresh reading of the live
    # tree. A file mutated, added or removed between the gate call and the copy --
    # including a live change that is restored again afterwards -- leaves the copy
    # disagreeing with what was admitted and fails packaging closed. The live
    # frontend\dist is never read again.
    $stagedDist = Join-Path $payload 'frontend\dist'
    $stagedDistCheck = & python (Join-Path $sourcePath 'scripts\release_gate.py') verify-staged-dist --staged $stagedDist --expect $admittedDist.dist_digest 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged frontend\dist is not the tree the gate admitted: $($stagedDistCheck -join ' ')"
    }
    Write-Host "Frontend dist staged $($stagedDistCheck -join ' ')"
    # ---- end admitted frontend dist ---------------------------------------
    Copy-Item -LiteralPath (Join-Path $sourcePath 'scripts\windows') -Destination (Join-Path $payload 'scripts\windows') -Recurse
    foreach ($file in @('run_work_stack.py', 'requirements.txt', 'requirements-windows-desktop.txt', 'README.md', 'LICENSE', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md')) {
        Copy-Item -LiteralPath (Join-Path $sourcePath $file) -Destination (Join-Path $payload $file)
    }
    Remove-PythonBytecode -Root $payload
    if (-not (Test-Path -LiteralPath (Join-Path $payload 'workstack\__init__.py') -PathType Leaf)) {
        throw 'Bundled Work Stack package was removed during bytecode cleanup.'
    }

    if ($RuntimeArchivePath) {
        $runtimeArchive = [IO.Path]::GetFullPath($RuntimeArchivePath)
        if (-not (Test-Path -LiteralPath $runtimeArchive -PathType Leaf)) {
            throw "Pinned Python runtime archive does not exist: $runtimeArchive"
        }
    } else {
        $runtimeArchive = Join-Path $temporary $runtimeFilename
        Invoke-WebRequest -UseBasicParsing -Uri $runtimeUrl -OutFile $runtimeArchive
    }
    $actualRuntimeSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $runtimeArchive).Hash.ToLowerInvariant()
    if ($actualRuntimeSha256 -ne $runtimeSha256) {
        throw "Pinned Python runtime hash mismatch. Expected $runtimeSha256, got $actualRuntimeSha256."
    }
    $runtime = Join-Path $payload 'runtime'
    Expand-Archive -LiteralPath $runtimeArchive -DestinationPath $runtime
    $runtimePython = Join-Path $runtime 'python.exe'
    if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        throw 'Pinned Python runtime has no runtime\python.exe.'
    }
    @(
        'python312.zip'
        '.'
        'Lib\site-packages'
        '..'
    ) | Set-Content -LiteralPath (Join-Path $runtime 'python312._pth') -Encoding ascii

    $sitePackages = Join-Path $runtime 'Lib\site-packages'
    New-Item -ItemType Directory -Path $sitePackages | Out-Null
    if (-not $SkipWheelDownload) {
        $wheels = Join-Path $payload 'wheels'
        New-Item -ItemType Directory -Path $wheels | Out-Null
        python -m pip download --disable-pip-version-check --only-binary=:all: --require-hashes -r (Join-Path $sourcePath 'requirements.txt') -d $wheels
        if ($LASTEXITCODE -ne 0) { throw 'Locked wheel download failed.' }
        python -m pip download --disable-pip-version-check --no-deps --require-hashes -r (Join-Path $sourcePath 'requirements-windows-desktop.txt') -d $wheels
        if ($LASTEXITCODE -ne 0) { throw 'Locked desktop dependency download failed.' }
    } else {
        $sourceWheels = Join-Path $sourcePath 'wheels'
        if (-not (Test-Path -LiteralPath $sourceWheels -PathType Container)) {
            throw '-SkipWheelDownload requires a source wheels directory.'
        }
        $wheels = Join-Path $payload 'wheels'
        Copy-Item -LiteralPath $sourceWheels -Destination $wheels -Recurse
    }
    $buildTools = Join-Path $temporary 'build-tools'
    python -m pip install --disable-pip-version-check --only-binary=:all: --no-deps --require-hashes --target $buildTools -r (Join-Path $sourcePath 'requirements-windows-build.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Locked installer build dependency installation failed.' }
    $previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
    try {
        $buildPythonPath = $buildTools
        if ($previousPythonPath) {
            $buildPythonPath += [IO.Path]::PathSeparator + $previousPythonPath
        }
        [Environment]::SetEnvironmentVariable('PYTHONPATH', $buildPythonPath, 'Process')
        python -c "import setuptools.build_meta"
        if ($LASTEXITCODE -ne 0) { throw 'Locked installer build backend is unavailable.' }
        python -m pip install --disable-pip-version-check --no-index --find-links $wheels --no-deps --no-build-isolation --require-hashes --target $sitePackages -r (Join-Path $sourcePath 'requirements.txt') -r (Join-Path $sourcePath 'requirements-windows-desktop.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Locked runtime dependency installation failed.' }
    } finally {
        [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
    }
    $bundledPackage = Join-Path $payload 'workstack\__init__.py'
    if (-not (Test-Path -LiteralPath $bundledPackage -PathType Leaf)) {
        throw "Bundled Work Stack package is missing: $bundledPackage"
    }
    & $runtimePython -c "import sys; sys.path.insert(0, sys.argv[1]); import jsonschema,struct,unicodedata2,workstack,webview; from pathlib import Path; from webview.platforms.edgechromium import WebView2; assert Path(workstack.__file__).resolve() == (Path(sys.argv[1]) / 'workstack' / '__init__.py').resolve(); assert f'{sys.version_info.major}.{sys.version_info.minor}:{struct.calcsize(chr(80))*8}' == '3.12:64'; assert unicodedata2.unidata_version == '17.0.0'; print(workstack.__version__)" $payload
    if ($LASTEXITCODE -ne 0) {
        throw 'Bundled Python runtime smoke test failed.'
    }
    $registrySmoke = Join-Path $payload 'scripts\windows\Test-WorkStackConnectionRegistrySmoke.py'
    & $runtimePython $registrySmoke --install-root $payload
    if ($LASTEXITCODE -ne 0) {
        throw 'Bundled connection registry startup smoke test failed.'
    }
    $dependencyBin = Join-Path $sitePackages 'bin'
    if (Test-Path -LiteralPath $dependencyBin -PathType Container) {
        # pip-generated console wrappers retain the temporary build interpreter path.
        # Work Stack imports these libraries and does not ship or invoke their CLIs.
        Remove-Item -LiteralPath $dependencyBin -Recurse -Force
    }
    # ---- branded same-process desktop host ------------------------------
    # One bounded compile of the already-read host source into payload/WorkStack.exe.
    # No compiler is discovered, downloaded or installed, and the output is never
    # executed here. A missing compiler, source or icon fails packaging.
    $compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        throw "The pinned Framework64 C# compiler is missing: $compiler"
    }
    $hostSource = Join-Path $payload 'desktop\python-webview-shell\WorkStackHost.cs'
    if (-not (Test-Path -LiteralPath $hostSource -PathType Leaf)) {
        throw 'The Work Stack desktop host source is missing from the payload.'
    }
    $hostIcon = Get-WorkStackShortcutIconPath -InstallPath $payload
    if (-not (Test-Path -LiteralPath $hostIcon -PathType Leaf)) {
        throw "The packaged Work Stack icon is missing: $hostIcon"
    }

    # The single version truth is the already-read $version from workstack/__init__.py.
    # It is validated numerically before it is ever emitted into C#.
    $versionMatch = [regex]::Match($version, '^(\d{1,5})\.(\d{1,5})\.(\d{1,5})$')
    if (-not $versionMatch.Success) {
        throw "Work Stack version is not a plain three-part release version: $version"
    }
    $assemblyVersion = '{0}.0' -f $version

    $metadataSource = Join-Path $temporary 'WorkStackHostMetadata.cs'
    $metadataLines = @(
        'using System.Reflection;',
        '[assembly: AssemblyTitle("Work Stack")]',
        '[assembly: AssemblyDescription("Work Stack")]',
        '[assembly: AssemblyProduct("Work Stack")]',
        '[assembly: AssemblyCompany("Work Stack")]',
        ('[assembly: AssemblyVersion("{0}")]' -f $assemblyVersion),
        ('[assembly: AssemblyFileVersion("{0}")]' -f $assemblyVersion),
        ('[assembly: AssemblyInformationalVersion("{0}")]' -f $version)
    )
    Set-Content -LiteralPath $metadataSource -Value $metadataLines -Encoding utf8

    $hostOutput = Join-Path $payload 'WorkStack.exe'
    # Every path-bearing value is encoded with Windows argument quoting before
    # Start-Process serializes the list, so spaces, Unicode, embedded quotes and
    # trailing backslashes survive intact.
    $compilerArguments = @(
        '/nologo',
        '/target:winexe',
        '/platform:x64',
        '/optimize+',
        '/codepage:65001',
        ('/win32icon:' + (ConvertTo-WorkStackCommandLineArgument -Value $hostIcon)),
        ('/out:' + (ConvertTo-WorkStackCommandLineArgument -Value $hostOutput)),
        (ConvertTo-WorkStackCommandLineArgument -Value $hostSource),
        (ConvertTo-WorkStackCommandLineArgument -Value $metadataSource)
    )
    # Bounded: one invocation, no retry and no alternate compiler. The process
    # object is retained so a timeout can be reported rather than silently ignored.
    $compile = Start-Process -FilePath $compiler -ArgumentList $compilerArguments -NoNewWindow -PassThru -Wait:$false
    # Windows PowerShell 5.1 Start-Process -PassThru does not keep an associated
    # OS handle unless Handle is read while the compiler is still running.
    # Without it, WaitForExit can return true and ExitCode stays $null, which
    # -ne 0 treats as failure after a successful compile. This read does not
    # wait, kill, or decide success.
    try { [void]($compile.Handle) } catch { }
    $initialExited = $false
    try {
        $initialExited = [bool]$compile.WaitForExit(30000)
    } catch {
        # Exit is unknown, so the tree may still be in use: preserve it and report.
        $script:WorkStackPreserveTemporary = $true
        throw ("Waiting for the Work Stack desktop host compiler failed, so its exit is unknown and the " +
            "temporary tree at " + $temporary + " was preserved. " + $_.Exception.Message)
    }
    if (-not $initialExited) {
        # One bounded owned lifetime: a single termination attempt against this
        # exact instance, then a bounded confirmation. Nothing is scanned, no
        # descendant is touched and there is no second attempt.
        $terminationError = ''
        try { $compile.Kill() } catch { $terminationError = $_.Exception.Message }
        $confirmed = $false
        try { $confirmed = [bool]$compile.WaitForExit(5000) } catch { $terminationError += ' ' + $_.Exception.Message }
        if (-not $confirmed) {
            # Exit could not be established, so the temporary tree may still be in
            # use. It is preserved deliberately and the uncertainty is reported
            # alongside the primary failure rather than being cleaned up blindly.
            $script:WorkStackPreserveTemporary = $true
            throw ("Compiling the Work Stack desktop host timed out after 30 seconds, and its exit could not be " +
                "confirmed within 5 seconds; the temporary tree at $temporary was preserved. $terminationError")
        }
        throw 'Compiling the Work Stack desktop host timed out after 30 seconds.'
    }
    if ($null -eq $compile.ExitCode) {
        $script:WorkStackPreserveTemporary = $true
        throw ("Compiling the Work Stack desktop host finished, but its exit code is unknown so the " +
            "temporary tree at " + $temporary + " was preserved.")
    }
    if ($compile.ExitCode -ne 0) {
        throw "Compiling the Work Stack desktop host failed with exit code $($compile.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $hostOutput -PathType Leaf)) {
        throw 'The Work Stack desktop host was not produced.'
    }
    # Deterministic here means fixed validated inputs, argv, version, resource and
    # output selection. It is NOT a claim of byte-reproducible PE output, and no
    # reproducible-build switch is assumed of this legacy compiler.

    # ---- admitted Linux remote payload ----------------------------------
    # The Linux artifact is built by its own builder, on Linux, and is only
    # ever consumed here. This stage does not build, download, unpack or
    # re-hash it: it hands the selected archive and sidecar to the product's
    # own remote_provision_artifact admission -- the same gate the desktop
    # update flow runs before any SSH -- and only an admitted pair whose
    # product version is this build's version reaches the payload. The bytes
    # land under payload\remote at the canonical name derived from that
    # admitted identity, never from the input filename, and the copy on disk
    # is admitted again before it is packaged.
    $remotePayloadSummary = 'local-only, no Linux remote payload'
    if ($includesRemotePayload) {
        $remoteStager = Join-Path $sourcePath 'scripts\windows\Stage-WorkStackRemoteBundle.py'
        # A refusal is written to stderr, and merging that into the success
        # stream under 'Stop' would raise NativeCommandError at the call and
        # skip the exit-status decision below. The preference is relaxed for
        # exactly this one call so the refusal text is captured and the exit
        # code -- not the presence of stderr -- decides, then restored.
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $remoteStage = & python $remoteStager --source-root $sourcePath --payload $payload `
                --archive $LinuxArtifactArchivePath --sidecar $LinuxArtifactSidecarPath `
                --expect-version $version 2>&1
        } finally {
            $ErrorActionPreference = $previousErrorAction
        }
        if ($LASTEXITCODE -ne 0) {
            throw "The selected Linux remote artifact was not admitted: $($remoteStage -join ' ')"
        }
        $remoteSummaryLine = @($remoteStage | ForEach-Object { $_.ToString() } | Where-Object { $_.Trim() }) |
            Select-Object -Last 1
        $admittedRemote = $null
        try { $admittedRemote = $remoteSummaryLine | ConvertFrom-Json } catch { $admittedRemote = $null }
        if (-not $admittedRemote -or -not $admittedRemote.artifact_digest) {
            throw "The Linux remote payload stage reported no admitted artifact: $($remoteStage -join ' ')"
        }
        if ($admittedRemote.product_version -ne $version) {
            throw ("The staged Linux remote payload is $($admittedRemote.product_version), " +
                "but this Windows build is $version.")
        }
        $stagedRemoteArchive = Join-Path $payload ($admittedRemote.archive -replace '/', '\')
        if (-not (Test-Path -LiteralPath $stagedRemoteArchive -PathType Leaf)) {
            throw "The staged Linux remote archive is missing from the payload: $stagedRemoteArchive"
        }
        $remotePayloadSummary = ("includes Linux remote payload $($admittedRemote.archive) " +
            "$($admittedRemote.artifact_digest)")
        Write-Host "Remote payload admitted $($admittedRemote.archive) $($admittedRemote.artifact_digest)"
    }
    # ---- end admitted Linux remote payload -------------------------------

    Remove-PythonBytecode -Root $payload
    Compress-Archive -Path (Join-Path $payload '*') -DestinationPath $archive -CompressionLevel Optimal
    $encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes($archive))
    $stub = @'
[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\WorkStack",
    [string]$StateRoot = "$env:LOCALAPPDATA\WorkStack",
    [string]$DataDir = "$env:LOCALAPPDATA\WorkStack\data",
    [string]$BackupDir = '',
    [int]$Port = 8765,
    [int]$BackupRetention = 14,
    [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
$bundle = '__WORKSTACK_BUNDLE__'
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("workstack-setup-" + [guid]::NewGuid().ToString('N'))
$archive = Join-Path $temporary 'payload.zip'
$payload = Join-Path $temporary 'payload'
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    [IO.File]::WriteAllBytes($archive, [Convert]::FromBase64String($bundle))
    Expand-Archive -LiteralPath $archive -DestinationPath $payload
    $installer = Join-Path $payload 'scripts\windows\Install-WorkStack.ps1'
    $installerArguments = @{
        SourceRoot = $payload
        InstallRoot = $InstallRoot
        StateRoot = $StateRoot
        NoShortcut = $NoShortcut
    }
    foreach ($optionalName in @('DataDir', 'BackupDir', 'Port', 'BackupRetention')) {
        if ($PSBoundParameters.ContainsKey($optionalName)) {
            $installerArguments[$optionalName] = $PSBoundParameters[$optionalName]
        }
    }
    & $installer @installerArguments
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
'@
    $stub = $stub.Replace('__WORKSTACK_BUNDLE__', $encoded)
    Set-Content -LiteralPath $output -Value $stub -Encoding utf8
    $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $output).Hash.ToLowerInvariant()
    $checksumPath = "$output.sha256"
    $checksumLine = "$digest  $([IO.Path]::GetFileName($output))`n"
    [IO.File]::WriteAllText($checksumPath, $checksumLine, [Text.UTF8Encoding]::new($false))
    $manifestBuilder = Join-Path $sourcePath 'scripts\windows\New-WorkStackUpdateManifest.ps1'
    & $manifestBuilder -SetupPath $output -ChecksumPath $checksumPath
    Write-Host "Built $output"
    Write-Host "Checksum $checksumPath"
    Write-Host "SHA-256 $digest"
    Write-Host "Payload $remotePayloadSummary"
} finally {
    if ($script:WorkStackPreserveTemporary) {
        Write-Warning ("Preserving " + $temporary + ": a compiler instance may still be using it.")
    } elseif (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
