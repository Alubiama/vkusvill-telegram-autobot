@echo off
setlocal

set STARTUP_FILE=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\vkusvill-telegram-autobot-autostart.cmd

if exist "%STARTUP_FILE%" (
  echo Autostart is enabled.
  echo Startup file: %STARTUP_FILE%
  exit /b 0
)

echo Autostart is disabled.
exit /b 1
