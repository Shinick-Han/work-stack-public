[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath,
    [string]$ChecksumPath = ''
)

$ErrorActionPreference = 'Stop'
$setup = [IO.Path]::GetFullPath($SetupPath)
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Setup artifact does not exist: $setup"
}
if (-not $ChecksumPath) { $ChecksumPath = "$setup.sha256" }
$checksum = [IO.Path]::GetFullPath($ChecksumPath)
if (-not (Test-Path -LiteralPath $checksum -PathType Leaf)) {
    throw "Checksum sidecar does not exist: $checksum"
}

$checksumText = [IO.File]::ReadAllText($checksum, [Text.UTF8Encoding]::new($false))
$match = [regex]::Match($checksumText, '\A(?<digest>[0-9a-fA-F]{64})  (?<name>[^\r\n\\/]+)\r?\n?\z')
if (-not $match.Success) {
    throw 'Checksum sidecar must contain exactly one SHA-256 line with two spaces before the setup filename.'
}
$expectedName = [IO.Path]::GetFileName($setup)
$manifestName = $match.Groups['name'].Value
if (-not [string]::Equals($manifestName, $expectedName, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Setup filename mismatch. Sidecar names '$manifestName' but selected artifact is '$expectedName'."
}
$expectedDigest = $match.Groups['digest'].Value.ToLowerInvariant()
$actualDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLowerInvariant()
if ($actualDigest -ne $expectedDigest) {
    throw "Setup hash mismatch. Expected $expectedDigest, got $actualDigest."
}

Write-Output "VERIFIED SHA-256 $actualDigest  $expectedName"
