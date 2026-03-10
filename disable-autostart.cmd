@echo off
setlocal

set STARTUP_FILE=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\vkusvill-telegram-autobot-autostart.cmd
set TASK_ONLOGON=vkusvill-telegram-autobot-onlogon
set TASK_WATCHDOG=vkusvill-telegram-autobot-watchdog
if exist "%STARTUP_FILE%" (
  del /f /q "%STARTUP_FILE%"
)
schtasks /Delete /TN "%TASK_ONLOGON%" /F >nul 2>nul
schtasks /Delete /TN "%TASK_WATCHDOG%" /F >nul 2>nul
for /f "tokens=2 delims=," %%p in ('tasklist /V /FO CSV ^| findstr /I "watchdog-loop.cmd"') do (
  taskkill /PID %%~p /F >nul 2>nul
)

echo Autostart disabled.
exit /b 0
