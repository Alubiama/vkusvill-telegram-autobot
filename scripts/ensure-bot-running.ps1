$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
  Write-Output "venv python not found: $venvPython"
  exit 1
}

$normalizedPython = $venvPython.Replace("/", "\")
$pattern = "*$normalizedPython* -m src.main*"

$running = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like $pattern } |
  Select-Object -First 1

if ($null -ne $running) {
  Write-Output "bot already running: PID=$($running.ProcessId)"
  exit 0
}

Start-Process -FilePath $venvPython `
  -ArgumentList @("-m", "src.main") `
  -WorkingDirectory $projectDir `
  -WindowStyle Hidden

Write-Output "bot started"
exit 0
