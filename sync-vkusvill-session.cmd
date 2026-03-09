@echo off
setlocal

cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run start/auth first.
  exit /b 1
)

echo Exporting session via Chrome CDP. Please close all Chrome windows first.
".venv\Scripts\python.exe" scripts\vkusvill_export_state_cdp.py --profile-name "Default" --port 9222
exit /b %errorlevel%
