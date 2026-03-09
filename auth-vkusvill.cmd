@echo off
setlocal

cd /d %~dp0

echo Close all Google Chrome windows before continue.
echo Press any key to continue...
pause >nul

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto :error

".venv\Scripts\python.exe" scripts\vkusvill_auth_playwright.py --use-system-chrome-profile --chrome-profile-name "Default"
if errorlevel 1 goto :error

echo.
echo Auth flow finished.
exit /b 0

:error
echo.
echo Command failed with exit code %errorlevel%.
exit /b %errorlevel%
