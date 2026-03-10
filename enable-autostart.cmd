@echo off
setlocal

cd /d %~dp0

set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set STARTUP_FILE=%STARTUP_DIR%\vkusvill-telegram-autobot-autostart.cmd
set RUN_SCRIPT=%~dp0run-autostart.cmd
set WATCHDOG_SCRIPT=%~dp0scripts\ensure-bot-running.ps1
set WATCHDOG_LOOP=%~dp0watchdog-loop.cmd
set TASK_ONLOGON=vkusvill-telegram-autobot-onlogon
set TASK_WATCHDOG=vkusvill-telegram-autobot-watchdog

if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment is missing. Run start.cmd once first.
  exit /b 1
)

if not exist "%RUN_SCRIPT%" (
  echo Missing %RUN_SCRIPT%
  exit /b 1
)

if not exist "%WATCHDOG_SCRIPT%" (
  echo Missing %WATCHDOG_SCRIPT%
  exit /b 1
)

if not exist "%WATCHDOG_LOOP%" (
  echo Missing %WATCHDOG_LOOP%
  exit /b 1
)

if not exist "%STARTUP_DIR%" (
  echo Startup folder not found: %STARTUP_DIR%
  exit /b 1
)

(
  echo @echo off
  echo start "" /min cmd /c ""%WATCHDOG_LOOP%""
) > "%STARTUP_FILE%"

if errorlevel 1 (
  echo Failed to write startup file.
  exit /b 1
)

schtasks /Create /TN "%TASK_ONLOGON%" /TR "\"%RUN_SCRIPT%\"" /SC ONLOGON /F >nul
if errorlevel 1 (
  echo Warning: failed to create Task Scheduler task: %TASK_ONLOGON% (no rights^). Using Startup watchdog only.
)

schtasks /Create /TN "%TASK_WATCHDOG%" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"%WATCHDOG_SCRIPT%\"" /SC MINUTE /MO 5 /F >nul
if errorlevel 1 (
  echo Warning: failed to create Task Scheduler task: %TASK_WATCHDOG% (no rights^). Using Startup watchdog only.
)

echo Autostart enabled via Startup watchdog loop.
echo Startup file: %STARTUP_FILE%
echo Watchdog loop: %WATCHDOG_LOOP%
exit /b 0
