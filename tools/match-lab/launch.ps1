$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot 'python\python.exe'
$serverScript = Join-Path $PSScriptRoot 'server.py'
$runtimeRoot = Join-Path $env:TEMP 'MGA-match-lab'
$portFile = Join-Path $runtimeRoot 'port.txt'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Embedded Python was not found: $python"
}

function Get-MatchLabService {
    if (-not (Test-Path -LiteralPath $portFile)) { return $null }
    try {
        $portText = (Get-Content -LiteralPath $portFile -Raw).Trim()
        $port = 0
        if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { return $null }
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 1
        if ($health.service -eq 'MGA-Match-Lab' -and [int]$health.port -eq $port) {
            return [pscustomobject]@{ Port = $port; Health = $health }
        }
    } catch {
        return $null
    }
    return $null
}

$service = Get-MatchLabService
if ($service) {
    Write-Host "Stopping the previous Match Lab service so logs can appear in this window..."
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($service.Port)/api/shutdown" -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
    } catch {
        Write-Warning "Could not request a graceful shutdown: $($_.Exception.Message)"
    }
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-MatchLabService)) { break }
    }
    if (Get-MatchLabService) {
        throw 'The previous Match Lab service is still running. Close its PowerShell window and try again.'
    }
}

$null = New-Item -ItemType Directory -Path $runtimeRoot -Force
Write-Host 'Starting Maa Match Lab in this PowerShell window.'
Write-Host "Maa native logs: $(Join-Path $runtimeRoot 'logs')"
Write-Host 'The browser will open automatically. Screenshot errors and tracebacks will appear here.'

$pythonExitCode = $null
Push-Location $projectRoot
try {
    & $python $serverScript
    $pythonExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($pythonExitCode -ne 0) {
    throw "Maa Match Lab exited with code $pythonExitCode. Review the output above."
}
