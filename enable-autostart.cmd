@echo off
setlocal

cd /d %~dp0

set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set STARTUP_FILE=%STARTUP_DIR%\vkusvill-telegram-autobot-autostart.cmd
set RUN_SCRIPT=%~dp0run-autostart.cmd

if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment is missing. Run start.cmd once first.
  exit /b 1
)

if not exist "%RUN_SCRIPT%" (
  echo Missing %RUN_SCRIPT%
  exit /b 1
)

if not exist "%STARTUP_DIR%" (
  echo Startup folder not found: %STARTUP_DIR%
  exit /b 1
)

(
  echo @echo off
  echo start "" /min cmd /c ""%RUN_SCRIPT%""
) > "%STARTUP_FILE%"

if errorlevel 1 (
  echo Failed to write startup file.
  exit /b 1
)

echo Autostart enabled via Startup folder.
echo Startup file: %STARTUP_FILE%
exit /b 0
