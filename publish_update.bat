@echo off
rem Builds an update of Schedule Manager and emails it to yourself so your other computers can install it.
rem Usage: publish_update.bat "what changed"
cd /d "%~dp0"
if not exist ".buildvenv\Scripts\python.exe" (
  echo Run build_installer.bat first; it sets up the build tools and makes the installer your other computers need.
  pause
  exit /b 1
)
".buildvenv\Scripts\python.exe" installer\publish.py %*
echo.
pause
