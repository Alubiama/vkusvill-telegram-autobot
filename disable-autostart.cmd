@echo off
setlocal

set STARTUP_FILE=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\vkusvill-telegram-autobot-autostart.cmd

if not exist "%STARTUP_FILE%" (
  echo Autostart is already disabled.
  exit /b 0
)

del /f /q "%STARTUP_FILE%"
if errorlevel 1 (
  echo Failed to remove startup file.
  exit /b 1
)

echo Autostart disabled.
exit /b 0
