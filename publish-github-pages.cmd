@echo off
setlocal

set GH=C:\Program Files\GitHub CLI\gh.exe
set GIT=C:\Program Files\Git\cmd\git.exe

if not exist "%GH%" (
  echo GitHub CLI not found at "%GH%"
  exit /b 1
)
if not exist "%GIT%" (
  echo Git not found at "%GIT%"
  exit /b 1
)

cd /d %~dp0

echo Checking GitHub auth...
"%GH%" auth status >nul 2>&1
if errorlevel 1 (
  echo You are not logged in to GitHub. Opening web login...
  "%GH%" auth login --hostname github.com --web --git-protocol https
  if errorlevel 1 (
    echo GitHub login failed or was cancelled.
    exit /b 1
  )
)

set REPO_NAME=%~n0
set REPO_NAME=vkusvill-telegram-autobot

echo Initializing git metadata...
"%GIT%" rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  "%GIT%" init
)

"%GIT%" checkout -B main
"%GIT%" config --get user.name >nul 2>&1
if errorlevel 1 (
  for /f "delims=" %%l in ('"%GH%" api user -q ".login"') do set GH_LOGIN=%%l
  "%GIT%" config user.name %GH_LOGIN%
  "%GIT%" config user.email %GH_LOGIN%@users.noreply.github.com
)
"%GIT%" add .
"%GIT%" commit -m "feat: Telegram showcase + Mini App + GitHub Pages deploy" >nul 2>&1

echo Ensuring GitHub repo exists...
"%GH%" repo view "%REPO_NAME%" >nul 2>&1
if errorlevel 1 (
  "%GH%" repo create "%REPO_NAME%" --public --source . --remote origin --push
  if errorlevel 1 exit /b 1
) else (
  "%GIT%" remote get-url origin >nul 2>&1
  if errorlevel 1 (
    for /f "delims=" %%r in ('"%GH%" repo view "%REPO_NAME%" --json url -q ".url"') do set REPO_URL=%%r
    "%GIT%" remote add origin %REPO_URL%
  )
  "%GIT%" push -u origin main
)

if not defined GH_LOGIN (
  for /f "delims=" %%l in ('"%GH%" api user -q ".login"') do set GH_LOGIN=%%l
)
set PAGE_URL=https://%GH_LOGIN%.github.io/%REPO_NAME%/
echo.
echo Repo ready. GitHub Pages URL (after workflow completes):
echo %PAGE_URL%
echo.
echo Next:
echo 1) Wait 1-2 minutes for Actions -> Pages deploy.
echo 2) Put this URL into .env as MINI_APP_URL.
echo 3) Restart bot and run /app in Telegram.

exit /b 0
