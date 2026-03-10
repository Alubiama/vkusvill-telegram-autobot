@echo off
setlocal

cd /d %~dp0

if not exist "out" mkdir out
if not exist ".venv\Scripts\python.exe" (
  echo [%date% %time%] venv python missing >> out\watchdog.log
  exit /b 1
)
if not exist "scripts\ensure-bot-running.ps1" (
  echo [%date% %time%] ensure script missing >> out\watchdog.log
  exit /b 1
)

:loop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-bot-running.ps1" >> out\watchdog.log 2>&1
timeout /t 300 /nobreak >nul
goto loop
