Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $base

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt | Out-Null
& ".\.venv\Scripts\python.exe" -m playwright install chromium | Out-Null
& ".\.venv\Scripts\python.exe" "scripts\vkusvill_auth_playwright.py"
