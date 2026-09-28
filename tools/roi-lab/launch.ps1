$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot 'python\python.exe'
$serverScript = Join-Path $projectRoot 'tools\match-lab\server.py'
$runtimeRoot = Join-Path $env:TEMP 'MGA-roi-lab'
$portFile = Join-Path $runtimeRoot 'port.txt'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Embedded Python was not found: $python"
}

function Get-RoiLabService {
    if (-not (Test-Path -LiteralPath $portFile)) { return $null }
    try {
        $portText = (Get-Content -LiteralPath $portFile -Raw).Trim()
        $port = 0
        if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { return $null }
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 1
        if ($health.service -eq 'MGA-ROI-Lab' -and [int]$health.port -eq $port) {
            return [pscustomobject]@{ Port = $port }
        }
    } catch {
        return $null
    }
    return $null
}

$service = Get-RoiLabService
if ($service) {
    Write-Host 'Stopping the previous ROI Lab service...'
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($service.Port)/api/shutdown" -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
    } catch {
        Write-Warning "Could not request a graceful shutdown: $($_.Exception.Message)"
    }
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-RoiLabService)) { break }
    }
    if (Get-RoiLabService) {
        throw 'The previous ROI Lab service is still running. Close its PowerShell window and try again.'
    }
}

$null = New-Item -ItemType Directory -Path $runtimeRoot -Force
Write-Host 'Starting the standalone Maa ROI tool in this PowerShell window.'
Write-Host "Maa native logs: $(Join-Path $runtimeRoot 'logs')"
Write-Host 'The browser will open automatically. Screenshot errors will appear here.'

$pythonExitCode = $null
Push-Location $projectRoot
try {
    & $python $serverScript --roi-only
    $pythonExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($pythonExitCode -ne 0) {
    throw "ROI Lab exited with code $pythonExitCode. Review the output above."
}
