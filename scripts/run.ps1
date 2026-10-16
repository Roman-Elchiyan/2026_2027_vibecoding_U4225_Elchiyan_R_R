param([ValidateSet('polling','webhook')][string]$Mode = 'polling', [switch]$AI)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
& $pythonPath scripts/prepare_services.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($AI) {
    $env:ENABLE_AI = 'true'
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    docker compose up -d --wait --wait-timeout 90
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
if ($Mode -eq 'webhook') {
    & $pythonPath scripts/ngrok_agent.py start
    if ($LASTEXITCODE -ne 0) { $Mode = 'polling' }
}
try {
    & $pythonPath bot.py --stage lab3 --external --mode $Mode
} finally {
    if ($Mode -eq 'webhook') { & $pythonPath scripts/ngrok_agent.py stop }
}
