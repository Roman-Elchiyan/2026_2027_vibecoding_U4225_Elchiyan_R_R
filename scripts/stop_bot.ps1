$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'runtime/bot.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { exit 0 }
$botProcessId = [int](Get-Content -LiteralPath $pidPath)
$processes = @(Get-CimInstance Win32_Process)
$owned = @($processes | Where-Object { $_.ProcessId -eq $botProcessId -and $_.Name -in @('python.exe','pythonw.exe') })
if ($owned.Count -eq 0) { exit 0 }
# A Windows venv launcher can spawn the real Python process; stop that tree only.
$pending = @($botProcessId)
$descendants = @()
while ($pending.Count -gt 0) {
    $children = @($processes | Where-Object { $_.ParentProcessId -in $pending })
    $pending = @($children | ForEach-Object { [int]$_.ProcessId })
    $descendants += $pending
}
foreach ($processIdToStop in @($descendants) + @($botProcessId)) {
    Stop-Process -Id $processIdToStop -ErrorAction SilentlyContinue
}
Write-Output 'Owned bot process stopped.'
