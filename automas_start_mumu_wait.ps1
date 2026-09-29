$ErrorActionPreference = "Stop"

$mumu = "C:\Program Files\Netease\MuMu\nx_main\mumu-cli.exe"
$adb = "C:\Program Files\Netease\MuMu\nx_main\adb.exe"
$vmIndex = "0"
$adbSerial = "127.0.0.1:16384"

Write-Host "[MGA] Starting MuMu vmindex $vmIndex..."
& $mumu control --vmindex $vmIndex launch | Out-Host

Write-Host "[MGA] Waiting for Android and ADB $adbSerial..."
for ($i = 1; $i -le 90; $i++) {
    $infoText = & $mumu info --vmindex $vmIndex
    $androidStarted = $false
    try {
        $info = $infoText | ConvertFrom-Json
        $androidStarted = [bool]$info.is_android_started
    } catch {
        $androidStarted = $infoText -match '"is_android_started"\s*:\s*true'
    }

    if ($androidStarted) {
        & $adb connect $adbSerial | Out-Null
        & $adb -s $adbSerial shell echo ready | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[MGA] MuMu Android and ADB are ready."
            exit 0
        }
    }

    Start-Sleep -Seconds 2
}

Write-Host "[MGA] Timed out waiting for MuMu Android or ADB."
exit 1
