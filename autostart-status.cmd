@echo off
setlocal

set STARTUP_FILE=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\vkusvill-telegram-autobot-autostart.cmd
set TASK_ONLOGON=vkusvill-telegram-autobot-onlogon
set TASK_WATCHDOG=vkusvill-telegram-autobot-watchdog
set STATUS=1

if exist "%STARTUP_FILE%" (
  echo Startup folder: enabled.
  echo Startup file: %STARTUP_FILE%
  set STATUS=0
) else (
  echo Startup folder: disabled.
)

schtasks /Query /TN "%TASK_ONLOGON%" >nul 2>nul
if errorlevel 1 (
  echo Task %TASK_ONLOGON%: missing.
) else (
  echo Task %TASK_ONLOGON%: present.
  set STATUS=0
)

schtasks /Query /TN "%TASK_WATCHDOG%" >nul 2>nul
if errorlevel 1 (
  echo Task %TASK_WATCHDOG%: missing.
) else (
  echo Task %TASK_WATCHDOG%: present.
  set STATUS=0
)

wmic process where "name='cmd.exe' and commandline like '%%watchdog-loop.cmd%%'" get ProcessId /value | findstr /R /C:"ProcessId=[0-9]" >nul
if errorlevel 1 (
  echo Watchdog process: not running.
) else (
  echo Watchdog process: running.
  set STATUS=0
)

exit /b %STATUS%
