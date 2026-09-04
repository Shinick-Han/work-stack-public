[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$OutputPath = '',
    [string]$RuntimeArchivePath = '',
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
    Copy-Item -LiteralPath (Join-Path $sourcePath 'frontend\dist') -Destination (Join-Path $payload 'frontend\dist') -Recurse
    Copy-Item -LiteralPath (Join-Path $sourcePath 'scripts\windows') -Destination (Join-Path $payload 'scripts\windows') -Recurse
    foreach ($file in @('run_work_stack.py', 'requirements.txt', 'requirements-windows-desktop.txt', 'README.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md')) {
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
    $compile.Refresh()
    $compilerExitCode = $compile.ExitCode
    if ($null -eq $compilerExitCode) {
        # Some hosts populate HasExited without ExitCode. The produced host is the
        # authority: a missing file still fails, a present file is treated as success.
        if (-not (Test-Path -LiteralPath $hostOutput -PathType Leaf)) {
            throw 'Compiling the Work Stack desktop host finished without an exit code and produced no host.'
        }
        $compilerExitCode = 0
    }
    if ($compilerExitCode -ne 0) {
        throw "Compiling the Work Stack desktop host failed with exit code $compilerExitCode."
    }
    if (-not (Test-Path -LiteralPath $hostOutput -PathType Leaf)) {
        throw 'The Work Stack desktop host was not produced.'
    }
    # Deterministic here means fixed validated inputs, argv, version, resource and
    # output selection. It is NOT a claim of byte-reproducible PE output, and no
    # reproducible-build switch is assumed of this legacy compiler.

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
} finally {
    if ($script:WorkStackPreserveTemporary) {
        Write-Warning ("Preserving " + $temporary + ": a compiler instance may still be using it.")
    } elseif (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
