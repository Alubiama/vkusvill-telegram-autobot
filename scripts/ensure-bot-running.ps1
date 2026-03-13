$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
  Write-Output "venv python not found: $venvPython"
  exit 1
}

$normalizedPython = $venvPython.Replace("/", "\").ToLowerInvariant()
$srcMainPattern = "*-m src.main*"

$allRunning = Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like $srcMainPattern }

$launchers = @()

foreach ($proc in $allRunning) {
  $exePath = ""
  if ($null -ne $proc.ExecutablePath) {
    $exePath = $proc.ExecutablePath.Replace("/", "\").ToLowerInvariant()
  }
  if ($exePath -eq $normalizedPython) {
    $launchers += $proc
  }
}

$healthy = @()
$stale = @()
$launcherIds = @($launchers | ForEach-Object { $_.ProcessId })

foreach ($proc in $allRunning) {
  if ($launcherIds -contains $proc.ProcessId) {
    $healthy += $proc
    continue
  }

  if ($launcherIds -contains $proc.ParentProcessId) {
    $healthy += $proc
    continue
  }

  $stale += $proc
}

foreach ($proc in $stale) {
  try {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
    Write-Output "stopped stale bot PID=$($proc.ProcessId) exe=$($proc.ExecutablePath)"
  } catch {
    Write-Output "failed to stop stale bot PID=$($proc.ProcessId): $($_.Exception.Message)"
  }
}

if ($launchers.Count -gt 1) {
  $launchers |
    Select-Object -Skip 1 |
    ForEach-Object {
      try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
        Write-Output "stopped duplicate launcher PID=$($_.ProcessId)"
      } catch {
        Write-Output "failed to stop duplicate launcher PID=$($_.ProcessId): $($_.Exception.Message)"
      }
    }
  $primaryLauncher = $launchers | Select-Object -First 1
  $healthy = @(
    $allRunning | Where-Object {
      $_.ProcessId -eq $primaryLauncher.ProcessId -or $_.ParentProcessId -eq $primaryLauncher.ProcessId
    }
  )
}

if ($healthy.Count -ge 1) {
  $summary = ($healthy | ForEach-Object { $_.ProcessId }) -join ","
  Write-Output "bot already running: PID(s)=$summary"
  exit 0
}

Start-Process -FilePath $venvPython `
  -ArgumentList @("-m", "src.main") `
  -WorkingDirectory $projectDir `
  -WindowStyle Hidden

Write-Output "bot started"
exit 0
