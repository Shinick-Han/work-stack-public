[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$sdkVersion = '1.0.4129.50'
$sdkSha256 = 'd3934f482d484b89fb4825df720c710664e1143a1e90f7b3a60794ef33f473d2'
$sdkUrl = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$sdkVersion/microsoft.web.webview2.$sdkVersion.nupkg"
if (-not $SourceRoot) { $SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
$sourcePath = [IO.Path]::GetFullPath($SourceRoot)
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $sourcePath '.artifacts\webview2-shell' }
$outputPath = [IO.Path]::GetFullPath($OutputDirectory)
$cachePath = Join-Path $env:LOCALAPPDATA "WorkStackBuildCache\WebView2\$sdkVersion"
$packagePath = Join-Path $cachePath "microsoft.web.webview2.$sdkVersion.nupkg"
$zipPath = Join-Path $cachePath "microsoft.web.webview2.$sdkVersion.zip"
$expandedPath = Join-Path $cachePath 'expanded'
New-Item -ItemType Directory -Force -Path $cachePath, $outputPath | Out-Null

if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
    Invoke-WebRequest -UseBasicParsing -Uri $sdkUrl -OutFile $packagePath
}
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $packagePath).Hash.ToLowerInvariant()
if ($actualHash -ne $sdkSha256) {
    throw "WebView2 SDK hash mismatch. Expected $sdkSha256, got $actualHash."
}
if (-not (Test-Path -LiteralPath $expandedPath -PathType Container)) {
    Copy-Item -LiteralPath $packagePath -Destination $zipPath
    Expand-Archive -LiteralPath $zipPath -DestinationPath $expandedPath
}

$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw '.NET Framework 4.8 x64 C# compiler is unavailable.'
}
$core = Join-Path $expandedPath 'lib\net462\Microsoft.Web.WebView2.Core.dll'
$winForms = Join-Path $expandedPath 'lib\net462\Microsoft.Web.WebView2.WinForms.dll'
$loader = Join-Path $expandedPath 'runtimes\win-x64\native\WebView2Loader.dll'
foreach ($required in @($core, $winForms, $loader)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "WebView2 SDK file is missing: $required" }
}

$source = Join-Path $PSScriptRoot 'WorkStackShell.cs'
$executable = Join-Path $outputPath 'WorkStackShell.exe'
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /out:$executable `
    /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll `
    /reference:$core /reference:$winForms $source
if ($LASTEXITCODE -ne 0) { throw 'Work Stack WebView2 shell compilation failed.' }

Copy-Item -LiteralPath $core, $winForms, $loader -Destination $outputPath -Force
@'
<?xml version="1.0" encoding="utf-8" ?>
<configuration>
  <startup useLegacyV2RuntimeActivationPolicy="true">
    <supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.8" />
  </startup>
</configuration>
'@ | Set-Content -LiteralPath "$executable.config" -Encoding utf8

$digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $executable).Hash.ToLowerInvariant()
Write-Host "Built $executable"
Write-Host "WebView2 SDK $sdkVersion ($sdkSha256)"
Write-Host "Executable SHA-256 $digest"
