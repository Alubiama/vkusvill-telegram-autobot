@echo off
setlocal

cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo venv not found. Run start first.
  exit /b 1
)

if not exist "data\chrome-user-data\Default" (
  echo Automation profile is missing. Run collect-vkusvill-discounts.cmd once and login.
  exit /b 1
)

if defined VKUSVILL_EXPECTED_DELIVERY (
  ".venv\Scripts\python.exe" scripts\vkusvill_collect_discounts.py --source system_chrome --chrome-user-data-dir "data/chrome-user-data" --chrome-profile-name "Default" --headless --interactive-login --waves 3 --require-distinct-waves --offers-ready-food-url "https://vkusvill.ru/offers/gotovaya-eda/" --offers-ready-food-max 0 --expected-delivery-hint "%VKUSVILL_EXPECTED_DELIVERY%" --strict-delivery-check --out-file data\today_discounts.json
) else (
  ".venv\Scripts\python.exe" scripts\vkusvill_collect_discounts.py --source system_chrome --chrome-user-data-dir "data/chrome-user-data" --chrome-profile-name "Default" --headless --interactive-login --waves 3 --require-distinct-waves --offers-ready-food-url "https://vkusvill.ru/offers/gotovaya-eda/" --offers-ready-food-max 0 --out-file data\today_discounts.json
)
exit /b %errorlevel%
