#Requires -Version 5.1
# Zip install\ -> dist\MGA-win-x86_64-<version>.zip (layout like MGA-win-x86_64-v0.1.1.zip).
# Skips privacy files (appsettings.json, *.user, etc.). Run build_install_windows.ps1 first.
# PR test zip: -Pr -Version mytag  ->  dist\MGA-win-x86_64-pr-mytag.zip
param(
    [string]$Version = "",
    [switch]$Pr,
    [string]$ZipName = "",
    [string]$InstallDir = "",
    [string]$OutDir = "",
    [switch]$FromRepoRoot
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

$install = if ($InstallDir) { $InstallDir } else { Join-Path $RepoRoot "install" }
$out = if ($OutDir) { $OutDir } else { Join-Path $RepoRoot "dist" }

if ($FromRepoRoot) {
    Write-Host "==> Package from: repo root (excludes .git, tools, deps, config, debug, docs, logs, ...)"
}
else {
    Write-Host "==> Package from: $install"
}
Write-Host "==> Output dir:   $out"

$py = Join-Path $RepoRoot "tools\CI\package_release.py"
$argList = if ($FromRepoRoot) {
    @("--from-repo-root", "--out", $out)
}
else {
    @("--install", $install, "--out", $out)
}

if ($ZipName) {
    $argList += "--zip-name", $ZipName
    if ($Version) { $argList += "--version", $Version }
}
elseif ($Pr) {
    if (-not $Version) { throw "With -Pr, -Version is required (e.g. test1 or 20250411)." }
    $safe = ($Version -replace '[^a-zA-Z0-9._-]', '_')
    $argList += "--zip-name", "MGA-win-x86_64-pr-$safe.zip"
    $argList += "--version", $Version
}
else {
    if (-not $Version) { throw "Specify -Version, or use -Pr -Version <tag>, or -ZipName <file>.zip" }
    $argList += "--version", $Version
}

Write-Host "==> Args: $($argList -join ' ')"
& python $py @argList
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Done."
