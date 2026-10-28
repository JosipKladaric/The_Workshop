param([int]$Port = 8080)
$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $workspacePath
$dataPath = Join-Path $workspacePath 'data'
New-Item -ItemType Directory -Force -Path $dataPath | Out-Null
$logPath = Join-Path $dataPath 'server.log'
if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
    Move-Item -LiteralPath $logPath -Destination ($logPath + '.previous') -Force
}
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -u run.py --host 0.0.0.0 --port $Port *>> $logPath
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -u run.py --host 0.0.0.0 --port $Port *>> $logPath
} else { throw 'Python is unavailable on PATH for automatic startup.' }
exit $LASTEXITCODE
