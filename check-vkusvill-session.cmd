@echo off
setlocal

cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run auth-vkusvill.cmd first.
  exit /b 1
)

".venv\Scripts\python.exe" scripts\vkusvill_session_check.py
exit /b %errorlevel%
