param([switch]$CheckOnly, [switch]$InstallForDirectExe)

$ErrorActionPreference = 'Stop'
$releaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$mgaExe = Join-Path $releaseDir 'MGA.exe'
if (-not (Test-Path -LiteralPath $mgaExe -PathType Leaf)) {
    Write-Error "MGA.exe was not found next to this launcher: $releaseDir"
    exit 1
}

$candidates = New-Object 'System.Collections.Generic.List[string]'
Get-Process -Name MuMuNxMain -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ($_.Path) {
            $candidates.Add((Join-Path (Split-Path -Parent $_.Path) 'adb.exe'))
        }
    } catch {}
}

foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if ($base) {
        $candidates.Add((Join-Path $base 'Netease\MuMu\nx_main\adb.exe'))
    }
}

foreach ($registryPath in @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)) {
    Get-ItemProperty -Path $registryPath -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'MuMu' -and $_.InstallLocation } |
        ForEach-Object { $candidates.Add((Join-Path $_.InstallLocation 'nx_main\adb.exe')) }
}

Get-Command adb.exe -All -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Source) { $candidates.Add($_.Source) }
}

$adbExe = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $adbExe) {
    Write-Error 'No adb.exe was found. Start an Android emulator or install Android platform-tools.'
    exit 1
}

$adbDir = Split-Path -Parent $adbExe
if ($InstallForDirectExe) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($userPath -split ';') -notcontains $adbDir) {
        $newPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
            $adbDir
        } else {
            $userPath.TrimEnd(';') + ';' + $adbDir
        }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MgaEnvBroadcast {
    [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)]
    public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, IntPtr wParam, string lParam, uint flags, uint timeout, out IntPtr result);
}
'@
    $result = [IntPtr]::Zero
    [MgaEnvBroadcast]::SendMessageTimeout([IntPtr]0xffff, 0x1a, [IntPtr]::Zero, 'Environment', 0x2, 5000, [ref]$result) | Out-Null
    Write-Host "ADB path added for this Windows user: $adbDir"
    Write-Host 'Close and reopen MGA.exe to use automatic device discovery.'
    exit 0
}

$env:PATH = $adbDir + ';' + $env:PATH
if ($adbExe -match 'Netease[\\/]MuMu') {
    & $adbExe connect 127.0.0.1:16384 | Out-Null
}

Write-Host "ADB: $adbExe"
& $adbExe devices
if ($CheckOnly) { exit 0 }

Start-Process -FilePath $mgaExe -WorkingDirectory $releaseDir
