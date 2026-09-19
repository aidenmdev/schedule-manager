@echo off
setlocal
cd /d "%~dp0"
title Schedule Manager - setup
echo Setting up Schedule Manager (only needed once per computer)...
echo.

set "BASEPY="
where py >nul 2>&1 && set "BASEPY=py -3"
if not defined BASEPY (
  where python >nul 2>&1 && set "BASEPY=python"
)
if not defined BASEPY (
  echo Python 3.10 or newer is required but was not found.
  echo Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

if exist ".venv" rmdir /s /q ".venv"
%BASEPY% -m venv ".venv"
if errorlevel 1 (
  echo Could not create the private Python environment.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installing packages failed. Check your internet connection and run this again.
  pause
  exit /b 1
)

echo.
echo Setup complete.
ping -n 3 127.0.0.1 >nul
exit /b 0
