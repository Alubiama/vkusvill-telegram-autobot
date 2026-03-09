@echo off
setlocal

cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run start first.
  exit /b 1
)

if not exist "data\chrome-user-data\Default" (
  echo Automation profile is missing. Run collect-vkusvill-discounts.cmd once and login.
  exit /b 1
)

".venv\Scripts\python.exe" scripts\vkusvill_collect_discounts.py --source system_chrome --chrome-user-data-dir "data/chrome-user-data" --chrome-profile-name "auto" --headless --out-file data\today_discounts.json
exit /b %errorlevel%
