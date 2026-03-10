@echo off
setlocal

cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run start/auth first.
  exit /b 1
)

if not exist "data\chrome-user-data\Default" (
  echo Initializing local automation Chrome profile...
  echo Close all regular Chrome windows and press any key.
  pause >nul
  ".venv\Scripts\python.exe" scripts\clone_chrome_profile.py --profile-name "Default" --dst-root "data/chrome-user-data"
  if errorlevel 1 exit /b %errorlevel%
)

echo Running discount collection from automation profile...
".venv\Scripts\python.exe" scripts\vkusvill_collect_discounts.py --source system_chrome --chrome-user-data-dir "data/chrome-user-data" --chrome-profile-name "Default" --interactive-login --waves 3 --offers-ready-food-url "https://vkusvill.ru/offers/gotovaya-eda/" --offers-ready-food-max 9 --out-file data\today_discounts.json
exit /b %errorlevel%
