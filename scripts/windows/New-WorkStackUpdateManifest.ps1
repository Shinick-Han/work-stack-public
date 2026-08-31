[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SetupPath,
    [string]$ChecksumPath = '',
    [string]$OutputPath = '',
    [ValidateRange(1, 65535)][int]$MinimumRemoteProtocol = 1,
    [DateTimeOffset]$PublishedAt = [DateTimeOffset]::UtcNow
)

$ErrorActionPreference = 'Stop'
$setup = [IO.Path]::GetFullPath($SetupPath)
if (-not $ChecksumPath) { $ChecksumPath = "$setup.sha256" }
$checksum = [IO.Path]::GetFullPath($ChecksumPath)
$name = [IO.Path]::GetFileName($setup)
$match = [regex]::Match($name, '\AWorkStack-Setup-(?<version>(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))\.ps1\z')
if (-not $match.Success) { throw 'Setup filename must be WorkStack-Setup-X.Y.Z.ps1.' }
$version = $match.Groups['version'].Value
$checksumName = "$name.sha256"
if ([IO.Path]::GetFileName($checksum) -ne $checksumName) {
    throw "Checksum filename must be $checksumName."
}
if (-not $OutputPath) { $OutputPath = Join-Path (Split-Path -Parent $setup) 'workstack-update.json' }
$output = [IO.Path]::GetFullPath($OutputPath)

$verifier = Join-Path $PSScriptRoot 'Test-WorkStackSetup.ps1'
& $verifier -SetupPath $setup -ChecksumPath $checksum | Out-Null

$setupItem = Get-Item -LiteralPath $setup
$checksumItem = Get-Item -LiteralPath $checksum
$setupDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLowerInvariant()
$checksumDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $checksum).Hash.ToLowerInvariant()
$releaseBase = "https://github.com/Shinick-Han/work-stack-public/releases/download/v$version"
$manifest = [ordered]@{
    schema_version = 1
    channel = 'stable'
    version = $version
    published_at = $PublishedAt.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    release_url = "https://github.com/Shinick-Han/work-stack-public/releases/tag/v$version"
    minimum_remote_protocol = $MinimumRemoteProtocol
    installer = [ordered]@{
        name = $name
        url = "$releaseBase/$name"
        sha256 = $setupDigest
        size = [long]$setupItem.Length
    }
    checksum = [ordered]@{
        name = $checksumName
        url = "$releaseBase/$checksumName"
        sha256 = $checksumDigest
        size = [long]$checksumItem.Length
    }
}
$body = $manifest | ConvertTo-Json -Depth 4
$temporary = "$output.tmp-$PID"
try {
    [IO.File]::WriteAllText($temporary, $body + "`n", [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $output -Force
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
}

Write-Host "Update manifest $output"
Write-Host "Version $version"
Write-Host "Installer SHA-256 $setupDigest"
