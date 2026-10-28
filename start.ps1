param([switch]$Lan, [int]$Port = 8080)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$bindAddress = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 run.py --host $bindAddress --port $Port
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python run.py --host $bindAddress --port $Port
} else {
    Write-Host 'Python 3.11 or newer is required. Install Python with the launcher, then run this file again.'
    exit 1
}
exit $LASTEXITCODE
