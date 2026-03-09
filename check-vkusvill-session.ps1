Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $base

& ".\.venv\Scripts\python.exe" "scripts\vkusvill_session_check.py"
