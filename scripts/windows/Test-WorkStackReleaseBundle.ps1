[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundlePath
)

$ErrorActionPreference = 'Stop'

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead([IO.Path]::GetFullPath($Path))
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

$bundle = [IO.Path]::GetFullPath($BundlePath)
if (-not (Test-Path -LiteralPath $bundle -PathType Container)) {
    throw "Release bundle directory does not exist: $bundle"
}
$receiptPath = Join-Path $bundle 'build-receipt.json'
if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
    throw 'Release bundle has no build-receipt.json.'
}
$receipt = Get-Content -Raw -LiteralPath $receiptPath | ConvertFrom-Json
if ([int]$receipt.schema_version -ne 1) { throw 'Build receipt schema_version must be 1.' }
$version = [string]$receipt.version
if ($version -notmatch '\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\z') {
    throw 'Build receipt version is not canonical X.Y.Z.'
}
if ([string]$receipt.candidate_sha -notmatch '\A[0-9a-f]{40}\z' -or
    [string]$receipt.tree_sha -notmatch '\A[0-9a-f]{40}\z') {
    throw 'Build receipt source commit and tree must be full lowercase SHAs.'
}
$installerName = "WorkStack-Setup-$version.ps1"
$required = @(
    $installerName,
    "$installerName.sha256",
    'workstack-update.json',
    'frozen-dist-manifest.json',
    'Test-WorkStackReleaseBundle.ps1',
    'build-receipt.json'
) | Sort-Object
$actual = @(Get-ChildItem -LiteralPath $bundle -File | ForEach-Object Name | Sort-Object)
if (($required -join "`n") -ne ($actual -join "`n")) {
    throw "Release bundle file set mismatch. Expected $($required -join ', '); got $($actual -join ', ')."
}

$recorded = @{}
foreach ($payload in @($receipt.payloads)) {
    $name = [string]$payload.name
    if ($recorded.ContainsKey($name) -or $name -notin ($required | Where-Object { $_ -ne 'build-receipt.json' })) {
        throw "Unexpected or duplicate payload in build receipt: $name"
    }
    $path = Join-Path $bundle $name
    $item = Get-Item -LiteralPath $path
    $digest = Get-Sha256Hex -Path $path
    if ([long]$payload.size -ne [long]$item.Length -or [string]$payload.sha256 -cne $digest) {
        throw "Release payload hash mismatch: $name"
    }
    $recorded[$name] = $true
}
if ($recorded.Count -ne $required.Count - 1) { throw 'Build receipt omits a required payload.' }

$installerPath = Join-Path $bundle $installerName
$installerDigest = Get-Sha256Hex -Path $installerPath
$sidecar = [IO.File]::ReadAllText((Join-Path $bundle "$installerName.sha256"), [Text.UTF8Encoding]::new($false))
if ($sidecar -cne "$installerDigest  $installerName`n") {
    throw 'Checksum sidecar does not identify the exact installer digest.'
}
$update = Get-Content -Raw -LiteralPath (Join-Path $bundle 'workstack-update.json') | ConvertFrom-Json
if ([string]$update.version -cne $version -or [string]$update.installer.name -cne $installerName -or
    [string]$update.installer.sha256 -cne $installerDigest) {
    throw 'Update manifest does not identify the exact installer digest.'
}

Write-Output "VERIFIED RELEASE BUNDLE $version $installerDigest"
