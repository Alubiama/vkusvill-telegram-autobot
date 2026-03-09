@echo off
setlocal

cd /d %~dp0

if not exist "out" mkdir out
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv\Scripts\python.exe >> out\autostart.log
  exit /b 1
)

".venv\Scripts\python.exe" -m src.main >> out\autostart.log 2>&1
exit /b %errorlevel%
