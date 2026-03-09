@echo off
setlocal

cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run start first.
  exit /b 1
)

echo Exporting session state from system Chrome via CDP...
".venv\Scripts\python.exe" scripts\vkusvill_export_state_cdp.py --profile-name "Default" --port 9222
exit /b %errorlevel%
