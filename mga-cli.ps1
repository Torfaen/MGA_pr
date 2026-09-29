param(
    [Parameter(Mandatory = $true)]
    [Alias("n")]
    [string]$Name,

    [switch]$DryRun,

    [switch]$CleanStaleCli
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$embeddedPython = Join-Path $root "python\python.exe"
$python = if (Test-Path -LiteralPath $embeddedPython) { $embeddedPython } else { "python" }
$script = Join-Path $root "cli_start.py"

$argsList = @()
if ($Name) {
    $argsList += @("-n", $Name)
}
$argsList += "--export-config"
if ($DryRun) {
    $argsList += "--dry-run"
}
if ($CleanStaleCli) {
    $argsList += "--clean-stale-cli"
}

& $python $script @argsList
exit $LASTEXITCODE
